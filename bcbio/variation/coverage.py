"""Examine and query coverage in sequencing experiments.

Provides estimates of coverage intervals based on callable regions
"""
import collections
import csv
import itertools
import os
import shutil
import yaml

import pybedtools
import numpy as np
import pysam
import toolz as tz

from bcbio.utils import (append_stem, copy_plus)
from bcbio import bam, utils
from bcbio.bam import ref, readstats
from bcbio.distributed.transaction import file_transaction
from bcbio.log import logger
from bcbio.pipeline import datadict as dd
from bcbio.provenance import do
from bcbio.pipeline import shared
from bcbio.structural import regions

GENOME_COV_THRESH = 0.40  # percent of genome covered for whole genome analysis
OFFTARGET_THRESH = 0.01  # percent of offtarget reads required to be capture (not amplification) based
DEPTH_THRESHOLDS = [1, 5, 10, 20, 50, 100, 250, 500, 1000, 5000, 10000, 50000]


def assign_interval(data):
    """Identify coverage based on percent of genome covered and relation to targets.

    Classifies coverage into 3 categories:
      - genome: Full genome coverage
      - regional: Regional coverage, like exome capture, with off-target reads
      - amplicon: Amplication based regional coverage without off-target reads
    """
    if not dd.get_coverage_interval(data):
        vrs = dd.get_variant_regions_merged(data)
        callable_file = dd.get_sample_callable(data)
        if vrs:
            callable_size = pybedtools.BedTool(vrs).total_coverage()
        else:
            callable_size = pybedtools.BedTool(callable_file).total_coverage()
        total_size = sum([c.size for c in ref.file_contigs(dd.get_ref_file(data), data["config"])])
        genome_cov_pct = callable_size / float(total_size)
        if genome_cov_pct > GENOME_COV_THRESH:
            cov_interval = "genome"
            offtarget_pct = 0.0
        elif not vrs:
            cov_interval = "regional"
            offtarget_pct = 0.0
        else:
            offtarget_pct = _count_offtarget(data, dd.get_align_bam(data) or dd.get_work_bam(data),
                                             vrs or callable_file, "variant_regions")
            if offtarget_pct > OFFTARGET_THRESH:
                cov_interval = "regional"
            else:
                cov_interval = "amplicon"
        logger.info("%s: Assigned coverage as '%s' with %.1f%% genome coverage and %.1f%% offtarget coverage"
                    % (dd.get_sample_name(data), cov_interval, genome_cov_pct * 100.0, offtarget_pct * 100.0))
        data["config"]["algorithm"]["coverage_interval"] = cov_interval
    return data

def _count_offtarget(data, bam_file, bed_file, target_name):
    mapped_unique = readstats.number_of_mapped_reads(data, bam_file, keep_dups=False)
    ontarget = readstats.number_of_mapped_reads(
        data, bam_file, keep_dups=False, bed_file=bed_file, target_name=target_name)
    if mapped_unique:
        return float(mapped_unique - ontarget) / mapped_unique
    else:
        return 0.0

def calculate(bam_file, data):
    """Calculate coverage in parallel using mosdepth.

    Removes duplicates and secondary reads from the counts:
    if ( b->core.flag & (BAM_FUNMAP | BAM_FSECONDARY | BAM_FQCFAIL | BAM_FDUP) ) continue;
    """
    params = {"min": dd.get_coverage_depth_min(data)}
    variant_regions = dd.get_variant_regions_merged(data)
    if not variant_regions:
        variant_regions = _create_genome_regions(data)
    # Back compatible with previous pre-mosdepth callable files
    callable_file = os.path.join(utils.safe_makedir(os.path.join(dd.get_work_dir(data), "align",
                                                                 dd.get_sample_name(data))),
                                 "%s-coverage.callable.bed" % (dd.get_sample_name(data)))
    if not utils.file_uptodate(callable_file, bam_file):
        vr_quantize = ("0:1:%s:" % (params["min"]), ["NO_COVERAGE", "LOW_COVERAGE", "CALLABLE"])
        to_calculate = [("variant_regions", variant_regions, vr_quantize, None),
                        ("sv_regions", regions.get_sv_bed(data), None, None),
                        ("coverage", dd.get_coverage(data), None, DEPTH_THRESHOLDS)]
        depth_files = {}
        for target_name, region_bed, quantize, thresholds in to_calculate:
            if region_bed:
                cur_depth = {}
                depth_info = run_mosdepth(data, target_name, region_bed, quantize=quantize, thresholds=thresholds)
                for attr in ("dist", "regions", "thresholds"):
                    val = getattr(depth_info, attr, None)
                    if val:
                        cur_depth[attr] = val
                depth_files[target_name] = cur_depth
                if target_name == "variant_regions":
                    callable_file = depth_info.quantize
    else:
        depth_files = {}
    final_callable = _subset_to_variant_regions(callable_file, variant_regions, data)
    return final_callable, depth_files

def _create_genome_regions(data):
    """Create whole genome contigs we want to process, only non-alts.

    Skips problem contigs like HLAs for downstream analysis.
    """
    work_dir = utils.safe_makedir(os.path.join(dd.get_work_dir(data), "coverage", dd.get_sample_name(data)))
    variant_regions = os.path.join(work_dir, "target-genome.bed")
    with file_transaction(data, variant_regions) as tx_variant_regions:
        with open(tx_variant_regions, "w") as out_handle:
            for c in shared.get_noalt_contigs(data):
                out_handle.write("%s\t%s\t%s\n" % (c.name, 0, c.size))
    return variant_regions

def _subset_to_variant_regions(callable_file, variant_regions, data):
    """Subset output callable file to only variant regions of interest.
    """
    out_file = "%s-vrsubset.bed" % utils.splitext_plus(callable_file)[0]
    if not utils.file_uptodate(out_file, callable_file):
        with file_transaction(data, out_file) as tx_out_file:
            pybedtools.BedTool(callable_file).intersect(variant_regions).saveas(tx_out_file)
    return out_file

def _get_cache_file(data, target_name):
    prefix = os.path.join(
        utils.safe_makedir(os.path.join(dd.get_work_dir(data), "align", dd.get_sample_name(data))),
        "%s-coverage" % (dd.get_sample_name(data)))
    cache_file = prefix + "-" + target_name + "-stats.yaml"
    return cache_file

def _read_cache(cache_file, reuse_cmp_files):
    reuse_cmp_file = [fn for fn in reuse_cmp_files if fn]
    if all(utils.file_uptodate(cache_file, fn) for fn in reuse_cmp_file):
        with open(cache_file) as in_handle:
            return yaml.safe_load(in_handle)
    return dict()

def _write_cache(cache, cache_file):
    with open(cache_file, "w") as out_handle:
        yaml.safe_dump(cache, out_handle, default_flow_style=False, allow_unicode=False)

def get_average_coverage(target_name, bed_file, data, bam_file=None):
    if not bam_file:
        bam_file = dd.get_align_bam(data) or dd.get_work_bam(data)
    cache_file = _get_cache_file(data, target_name)
    cache = _read_cache(cache_file, [bam_file, bed_file])
    if "avg_coverage" in cache:
        return int(cache["avg_coverage"])

    if bed_file:
        avg_cov = _average_bed_coverage(bed_file, target_name, data)
    else:
        avg_cov = _average_genome_coverage(data, bam_file)

    cache["avg_coverage"] = int(avg_cov)
    _write_cache(cache, cache_file)
    return int(avg_cov)

def _average_genome_coverage(data, bam_file):
    """Quickly calculate average coverage for whole genome files using indices.

    Includes all reads, with duplicates.
    """
    total = sum([c.size for c in ref.file_contigs(dd.get_ref_file(data), data["config"])])
    read_counts = sum(x.aligned for x in bam.idxstats(bam_file, data))
    with pysam.Samfile(bam_file, "rb") as pysam_bam:
        read_size = np.median(list(itertools.islice((a.query_length for a in pysam_bam.fetch()), 1e5)))
    avg_cov = float(read_counts * read_size) / total
    return avg_cov

def _average_bed_coverage(bed_file, target_name, data):
    depth_file = regions_coverage(bed_file, target_name, data)
    avg_covs = []
    total_len = 0
    with utils.open_gzipsafe(depth_file) as fh:
        for line_tokens in (l.rstrip().split() for l in fh if not l.startswith("#")):
            line_tokens = [x for x in line_tokens if x.strip()]
            start, end = map(int, line_tokens[1:3])
            size = end - start
            avg_covs.append(float(line_tokens[-1]) * size)
            total_len += size
    avg_cov = sum(avg_covs) / total_len if total_len > 0 else 0
    return avg_cov

def _calculate_percentiles(dist_file, cutoffs, out_dir, data):
    """Calculate percentage over over specified cutoff range.

    XXX Does not calculate the per-bin coverage estimations which we had
    earlier with sambamba depth. Instead has a global metric of percent coverage
    which provides a more defined look at coverage changes by depth.
    """
    if not utils.file_exists(dist_file):
        return []
    sample = dd.get_sample_name(data)
    out_total_file = append_stem(dist_file, "_total_summary")
    if not utils.file_exists(out_total_file):
        with file_transaction(data, out_total_file) as tx_file:
            with open(tx_file, 'w') as out_handle:
                writer = csv.writer(out_handle, dialect="excel-tab")
                writer.writerow(["cutoff_reads", "bases_pct", "sample"])
                with open(dist_file) as in_handle:
                    for line in in_handle:
                        contig, count, pct = line.strip().split()
                        if contig == "total":
                            count = int(count)
                            pct = "%.1f" % (float(pct) * 100.0)
                            if count >= min(cutoffs) and count <= max(cutoffs):
                                writer.writerow(["percentage%s" % count, pct, sample])
                    if min(cutoffs) < count:
                        writer.writerow(["percentage%s" % min(cutoffs), pct, sample])
    # To move metrics to multiqc, will remove older files
    # when bcbreport accepts these one, to avoid errors
    # while porting everything to multiqc
    # These files will be copied to final
    out_total_fixed = os.path.join(os.path.dirname(out_total_file), "%s_bcbio_coverage_avg.txt" % sample)
    copy_plus(out_total_file, out_total_fixed)
    return [out_total_fixed]

def regions_coverage(bed_file, target_name, data):
    """Generate coverage over regions of interest using mosdepth.
    """
    ready_bed = tz.get_in(["depth", target_name, "regions"], data)
    if ready_bed:
        return ready_bed
    else:
        return run_mosdepth(data, target_name, bed_file).regions

def run_mosdepth(data, target_name, bed_file, per_base=False, quantize=None, thresholds=None):
    """Run mosdepth generating distribution, region depth and per-base depth.
    """
    MosdepthCov = collections.namedtuple("MosdepthCov", ("dist", "per_base", "regions", "quantize", "thresholds"))
    bam_file = dd.get_align_bam(data) or dd.get_work_bam(data)
    work_dir = utils.safe_makedir(os.path.join(dd.get_work_dir(data), "coverage", dd.get_sample_name(data)))
    prefix = os.path.join(work_dir, "%s-%s" % (dd.get_sample_name(data), target_name))
    out = MosdepthCov("%s.mosdepth.dist.txt" % prefix,
                      ("%s.per-base.bed.gz" % prefix) if per_base else None,
                      ("%s.regions.bed.gz" % prefix) if bed_file else None,
                      ("%s.quantized.bed.gz" % prefix) if quantize else None,
                      ("%s.thresholds.bed.gz" % prefix) if thresholds else None)
    if not utils.file_uptodate(out.dist, bam_file):
        with file_transaction(data, out.dist) as tx_out_file:
            tx_prefix = os.path.join(os.path.dirname(tx_out_file), os.path.basename(prefix))
            num_cores = dd.get_cores(data)
            bed_arg = ("--by %s" % bed_file) if bed_file else ""
            perbase_arg = "" if per_base else "--no-per-base"
            mapq_arg = "-Q 1" if (per_base or quantize) else ""
            if quantize:
                quant_arg = "--quantize %s" % quantize[0]
                quant_export = " && ".join(["export MOSDEPTH_Q%s=%s" % (i, x) for (i, x) in enumerate(quantize[1])])
                quant_export += " && "
            else:
                quant_arg, quant_export = "", ""
            thresholds = "-T " + ",".join([str(t) for t in thresholds]) if thresholds else ""
            cmd = ("{quant_export}mosdepth -t {num_cores} -F 1804 {mapq_arg} {perbase_arg} {bed_arg} {quant_arg} "
                   "{tx_prefix} {bam_file} {thresholds}")
            message = "Calculating coverage: %s %s" % (dd.get_sample_name(data), target_name)
            do.run(cmd.format(**locals()), message.format(**locals()))
            if out.per_base:
                shutil.move(os.path.join(os.path.dirname(tx_out_file), os.path.basename(out.per_base)), out.per_base)
            if out.regions:
                shutil.move(os.path.join(os.path.dirname(tx_out_file), os.path.basename(out.regions)), out.regions)
            if out.quantize:
                shutil.move(os.path.join(os.path.dirname(tx_out_file), os.path.basename(out.quantize)), out.quantize)
            if out.thresholds:
                shutil.move(os.path.join(os.path.dirname(tx_out_file), os.path.basename(out.thresholds)), out.thresholds)
    return out

def coverage_region_detailed_stats(target_name, bed_file, data, out_dir):
    """
    Calculate coverage at different completeness cutoff
    for region in coverage option.
    """
    if not bed_file or not utils.file_exists(bed_file):
        return []
    else:
        ready_depth = tz.get_in(["depth", target_name], data)
        cov_file = ready_depth["regions"]
        dist_file = ready_depth["dist"]
        thresholds_file = ready_depth["thresholds"]
        out_cov_file = os.path.join(out_dir, os.path.basename(cov_file))
        out_dist_file = os.path.join(out_dir, os.path.basename(dist_file))
        out_thresholds_file = os.path.join(out_dir, os.path.basename(thresholds_file))
        if not utils.file_uptodate(out_cov_file, cov_file):
            utils.copy_plus(cov_file, out_cov_file)
            utils.copy_plus(dist_file, out_dist_file)
            utils.copy_plus(dist_file, out_thresholds_file)
        out_files = _calculate_percentiles(out_dist_file, DEPTH_THRESHOLDS, out_dir, data)
        return [os.path.abspath(x) for x in out_files] + [out_cov_file, out_dist_file, out_thresholds_file]
