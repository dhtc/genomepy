"""Module-level functions."""
import os
import norns
import re

from appdirs import user_config_dir
from glob import glob
from genomepy.genome import Genome
from genomepy.provider import ProviderBase
from genomepy.plugin import get_active_plugins, init_plugins
from genomepy.utils import (
    generate_gap_bed,
    generate_fa_sizes,
    get_localname,
    sanitize_annotation,
    get_genome_dir,
    glob_ext_files,
)

config = norns.config("genomepy", default="cfg/default.yaml")


def manage_config(cmd):
    """Manage genomepy config file."""
    if cmd == "file":
        print(config.config_file)
    elif cmd == "show":
        with open(config.config_file) as f:
            print(f.read())
    elif cmd == "generate":
        fname = os.path.join(user_config_dir("genomepy"), "{}.yaml".format("genomepy"))

        if not os.path.exists(user_config_dir("genomepy")):
            os.makedirs(user_config_dir("genomepy"))

        with open(fname, "w") as fout:
            with open(config.config_file) as fin:
                fout.write(fin.read())
        print("Created config file {}".format(fname))


def list_available_genomes(provider=None):
    """
    List all available genomes.

    Parameters
    ----------
    provider : str, optional
        List genomes from specific provider. Genomes from all
        providers will be returned if not specified.

    Returns
    -------
    list with genome names
    """
    if provider:
        providers = [ProviderBase.create(provider)]
    else:
        # if provider is not specified search all providers
        providers = [ProviderBase.create(p) for p in ProviderBase.list_providers()]

    for p in providers:
        for row in p.list_available_genomes():
            yield [p.name] + list(row)


def list_available_providers():
    """
    List all available providers.

    Returns
    -------
    list with provider names
    """
    return ProviderBase.list_providers()


def _is_genome_dir(dirname):
    """
    Check if a directory contains a fasta file

    Parameters
    ----------
    dirname : str
        Directory name

    Returns
    ------
    bool
    """
    return len(glob("{}/*.fa".format(dirname))) > 0


def list_installed_genomes(genome_dir=None):
    """
    List all available genomes.

    Parameters
    ----------
    genome_dir : str
        Directory with installed genomes.

    Returns
    -------
    list with genome names
    """
    if not genome_dir:
        genome_dir = config.get("genome_dir", None)
    if not genome_dir:
        raise norns.exceptions.ConfigError("Please provide or configure a genome_dir")
    genome_dir = os.path.expanduser(genome_dir)

    return [
        f for f in os.listdir(genome_dir) if _is_genome_dir(os.path.join(genome_dir, f))
    ]


def search(term, provider=None):
    """
    Search for a genome.

     If provider is specified, search only that specific provider, else
     search all providers. Both the name and description are used for the
     search. Search term is case-insensitive.

    Parameters
    ----------
    term : str
        Search term, case-insensitive.

    provider : str , optional
        Provider name

    Yields
    ------
    tuple
        genome information (name/identfier and description)
    """
    if provider:
        providers = [ProviderBase.create(provider)]
    else:
        # if provider is not specified search all providers (except direct url)
        providers = [
            ProviderBase.create(p) for p in ProviderBase.list_providers() if p != "url"
        ]
    for p in providers:
        for row in p.search(term):
            yield [
                x.encode("latin-1") for x in list(row[:1]) + [p.name] + list(row[1:])
            ]


def install_genome(
    name,
    provider,
    genome_dir=None,
    localname=None,
    mask="soft",
    regex=None,
    invert_match=False,
    bgzip=None,
    annotation=False,
    only_annotation=False,
    skip_sanitizing=False,
    threads=1,
    force=False,
    **kwargs,
):
    """
    Install a genome.

    Parameters
    ----------
    name : str
        Genome name

    provider : str
        Provider name

    genome_dir : str , optional
        Where to store the fasta files

    localname : str , optional
        Custom name for this genome.

    mask : str , optional
        Default is 'soft', choices 'hard'/'soft/'none' for respective masking level.

    regex : str , optional
        Regular expression to select specific chromosome / scaffold names.

    invert_match : bool , optional
        Set to True to select all chromosomes that don't match the regex.

    bgzip : bool , optional
        If set to True the genome FASTA file will be compressed using bgzip.
        If not specified, the setting from the configuration file will be used.

    threads : int, optional
        Build genome index using multithreading (if supported).

    force : bool , optional
        Set to True to overwrite existing files.

    annotation : bool , optional
        If set to True, download gene annotation in BED and GTF format.

    only_annotation : bool , optional
        If set to True, only download the annotation files.

    skip_sanitizing : bool , optional
        If set to True, downloaded annotation files whose sequence names do not match
        with the (first header fields of) the genome.fa will not be corrected.

    kwargs : dict, optional
        Provider specific options.
        toplevel : bool , optional
            Ensembl only: Always download the toplevel genome. Ignores potential primary assembly.

        version : int, optional
            Ensembl only: Specify release version. Default is latest.

        to_annotation : text , optional
            URL only: direct link to annotation file.
            Required if this is not the same directory as the fasta.
    """
    genome_dir = get_genome_dir(genome_dir)
    localname = get_localname(name, localname)
    out_dir = os.path.join(genome_dir, localname)

    # download annotation if any of the annotation related flags are given
    if only_annotation or kwargs.get("to_annotation", False):
        annotation = True

    # Check if genome already exists, or if downloading is forced
    no_genome_found = not any(
        os.path.exists(fname) for fname in glob_ext_files(out_dir, "fa")
    )
    if (no_genome_found or force) and not only_annotation:
        # Download genome from provider
        p = ProviderBase.create(provider)
        p.download_genome(
            name,
            genome_dir,
            mask=mask,
            regex=regex,
            invert_match=invert_match,
            localname=localname,
            bgzip=bgzip,
            **kwargs,
        )

    # annotation_only cannot use sanitizing if no genome (and sizes) file was made earlier. Warn the user about this.
    no_annotation_found = not any(
        os.path.exists(fname) for fname in glob_ext_files(out_dir, "gtf")
    )
    no_genome_found = not any(
        os.path.exists(fname) for fname in glob_ext_files(out_dir, "fa")
    )
    if only_annotation and no_genome_found:
        assert skip_sanitizing, (
            "a genome file is required to sanitize your annotation (or check if it's required). "
            "Use the skip sanitizing flag (-s) if you wish to skip this step."
        )

    # generates a Fasta object and the index file
    if not no_genome_found:
        g = Genome(localname, genome_dir=genome_dir)

    # Generate sizes file if not found or if generation is forced
    sizes_file = os.path.join(out_dir, localname + ".fa.sizes")
    if (not os.path.exists(sizes_file) or force) and not only_annotation:
        generate_fa_sizes(glob_ext_files(out_dir, "fa")[0], sizes_file)

    # Generate gap file if not found or if generation is forced
    gap_file = os.path.join(out_dir, localname + ".gaps.bed")
    if (not os.path.exists(gap_file) or force) and not only_annotation:
        generate_gap_bed(glob_ext_files(out_dir, "fa")[0], gap_file)

    # If annotation is requested, check if annotation already exists, or if downloading is forced
    if (no_annotation_found or force) and annotation:
        # Download annotation from provider
        p = ProviderBase.create(provider)
        p.download_annotation(name, genome_dir, localname=localname, **kwargs)
        if not skip_sanitizing:
            sanitize_annotation(g)

    # Run all active plugins
    for plugin in get_active_plugins():
        plugin.after_genome_download(g, threads, force)

    generate_env()


def generate_exports():
    """Print export commands for setting environment variables."""
    env = []
    for name in list_installed_genomes():
        try:
            g = Genome(name)
            env_name = re.sub(r"[^\w]+", "_", name).upper()
            env.append("export {}={}".format(env_name, g.filename))
        except Exception:
            pass
    return env


def generate_env(fname=None):
    """Generate file with exports.

    By default this is in .config/genomepy/exports.txt.

    Parameters
    ----------
    fname: strs, optional
        Name of the output file.
    """
    if fname is None:
        config_dir = user_config_dir("genomepy")
        fname = os.path.join(config_dir, "exports.txt")
    fname = os.path.expanduser(fname)
    if not os.path.exists(config_dir):
        raise Exception(f"config directory {config_dir} does not exist!")

    with open(fname, "w") as fout:
        for env in generate_exports():
            fout.write("{}\n".format(env))


def manage_plugins(command, plugin_names=None):
    """Enable or disable plugins.
    """
    if plugin_names is None:
        plugin_names = []
    active_plugins = config.get("plugin", [])
    plugins = init_plugins()
    if command == "enable":
        for name in plugin_names:
            if name not in plugins:
                raise ValueError("Unknown plugin: {}".format(name))
            if name not in active_plugins:
                active_plugins.append(name)
    elif command == "disable":
        for name in plugin_names:
            if name in active_plugins:
                active_plugins.remove(name)
    elif command == "list":
        print("{:20}{}".format("plugin", "enabled"))
        for plugin in sorted(plugins):
            print(
                "{:20}{}".format(
                    plugin, {False: "", True: "*"}[plugin in active_plugins]
                )
            )
    else:
        raise ValueError("Invalid plugin command")
    config["plugin"] = active_plugins
    config.save()

    if command in ["enable", "disable"]:
        print("Enabled plugins: {}".format(", ".join(sorted(active_plugins))))
