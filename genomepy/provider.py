"""Genome providers."""
import sys
import requests
import re
import os
import norns
import time
import shutil
import tarfile
import subprocess as sp

from psutil import virtual_memory
from tempfile import TemporaryDirectory
from urllib.request import urlopen, urlretrieve, urlcleanup, URLError
from bucketcache import Bucket
from pyfaidx import Fasta
from appdirs import user_cache_dir

from genomepy import exceptions
from genomepy.utils import filter_fasta, get_localname, get_file_info, read_url
from genomepy.__about__ import __version__

my_cache_dir = os.path.join(user_cache_dir("genomepy"), __version__)
# Create .cache dir if it does not exist
if not os.path.exists(my_cache_dir):
    os.makedirs(my_cache_dir)

cached = Bucket(my_cache_dir, days=7)

config = norns.config("genomepy", default="cfg/default.yaml")


def download_and_generate_annotation(genome_dir, annot_url, localname):
    """download annotation file, convert to intermediate file and generate output files"""

    # create output directory if missing
    out_dir = os.path.join(genome_dir, localname)
    if not os.path.exists(out_dir):
        os.mkdir(out_dir)

    # download to tmp dir. Move files on completion.
    with TemporaryDirectory(dir=out_dir) as tmpdir:
        ext, gz = get_file_info(annot_url)
        annot_file = os.path.join(tmpdir, localname + ".annotation" + ext)
        urlretrieve(annot_url, annot_file)

        # unzip input file (if needed)
        if gz:
            cmd = "mv {0} {1} && gunzip -f {1}"
            sp.check_call(cmd.format(annot_file, annot_file + ".gz"), shell=True)

        # generate intermediate file (GenePred)
        pred_file = annot_file.replace(ext, ".gp")
        if "bed" in ext:
            cmd = "bedToGenePred {0} {1}"
        elif "gff" in ext:
            cmd = "gff3ToGenePred -geneNameAttr=gene {0} {1}"
        elif "gtf" in ext:
            cmd = "gtfToGenePred {0} {1}"
        elif "txt" in ext:
            # UCSC annotations only
            with open(annot_file) as f:
                cols = f.readline().split("\t")

            start_col = 1
            for i, col in enumerate(cols):
                if col == "+" or col == "-":
                    start_col = i - 1
                    break
            end_col = start_col + 10

            cmd = f"cat {{0}} | cut -f{start_col}-{end_col} > {{1}}"
        sp.check_call(cmd.format(annot_file, pred_file), shell=True)

        # generate gzipped gtf file (if required)
        gtf_file = annot_file.replace(ext, ".gtf")
        if "gtf" not in ext:
            cmd = "genePredToGtf file {0} {1} && gzip -f {1}"
            sp.check_call(cmd.format(pred_file, gtf_file), shell=True)

        # generate gzipped bed file (if required)
        bed_file = annot_file.replace(ext, ".bed")
        if "bed" not in ext:
            cmd = "genePredToBed {0} {1} && gzip -f {1}"
            sp.check_call(cmd.format(pred_file, bed_file), shell=True)

        # if input file was gtf/bed, gzip it
        if ext in [".gtf", ".bed"]:
            cmd = "gzip -f {}"
            sp.check_call(cmd.format(annot_file), shell=True)

        # transfer the files from the tmpdir to the genome_dir
        for f in [gtf_file + ".gz", bed_file + ".gz"]:
            src = f
            dst = os.path.join(out_dir, os.path.basename(f))
            shutil.move(src, dst)


def attempt_download_and_report_back(genome_dir, annot_url, localname):
    try:
        sys.stderr.write("Using {}\n".format(annot_url))
        download_and_generate_annotation(
            genome_dir=genome_dir, annot_url=annot_url, localname=localname
        )
        sys.stderr.write("Annotation download successful\n")

        # add log entry
        readme = os.path.join(genome_dir, localname, "README.txt")
        with open(readme, "a") as f:
            f.write("Annotation url: {}\n".format(annot_url))

    except Exception:
        sys.stderr.write(
            "\nCould not download {}\n".format(annot_url)
            + "If you think the annotation should be there, please file a bug report at:\n"
            + "https://github.com/vanheeringen-lab/genomepy/issues\n"
        )
        raise


class ProviderBase(object):
    """Provider base class.

    Use to get a list of available providers:
    >>> ProviderBase.list_providers()
    ['UCSC', 'NCBI', 'Ensembl']

    Create a provider:
    >>> p = ProviderBase.create("UCSC")
    >>> for name, desc in p.search("hg38"):
    ...     print(desc)
    Human Dec. 2013 (GRCh38/hg38) Genome at UCSC
    """

    _providers = {}
    name = None

    @classmethod
    def create(cls, name):
        """Create a provider based on the provider name.

        Parameters
        ----------
        name : str
            Name of the provider (eg. UCSC, Ensembl, ...)

        Returns
        -------
        provider : Provider instance
            Provider instance.
        """
        try:
            return cls._providers[name.lower()]()
        except KeyError:
            raise Exception("Unknown provider")

    @classmethod
    def register_provider(cls, provider):
        """Register method to keep list of providers."""

        def decorator(subclass):
            """Register as decorator function."""
            cls._providers[provider.lower()] = subclass
            subclass.name = provider.lower()
            return subclass

        return decorator

    @classmethod
    def list_providers(cls):
        """List available providers."""
        return cls._providers.keys()

    def list_install_options(self, name=None):
        """List provider specific install options"""
        if name is None:
            return {}
        elif name.lower() not in self._providers:
            raise Exception("Unknown provider")
        else:
            provider = self._providers[name.lower()]
            return provider.list_install_options(self)

    def __hash__(self):
        return hash(str(self.__class__))

    @staticmethod
    def safe(name):
        """Replace spaces with undescores."""
        return name.replace(" ", "_")

    @staticmethod
    def tar_to_bigfile(fname, outfile):
        """Convert tar of multiple FASTAs to one file."""
        fnames = []
        with TemporaryDirectory() as tmpdir:
            # Extract files to temporary directory
            with tarfile.open(fname) as tar:
                tar.extractall(path=tmpdir)
            for root, _, files in os.walk(tmpdir):
                fnames += [os.path.join(root, fname) for fname in files]

            # Concatenate
            with open(outfile, "w") as out:
                for infile in fnames:
                    for line in open(infile):
                        out.write(line)
                    os.unlink(infile)

    def download_genome(
        self,
        name,
        genome_dir,
        localname=None,
        mask="soft",
        regex=None,
        invert_match=False,
        bgzip=None,
        **kwargs,
    ):
        """
        Download a (gzipped) genome file to a specific directory

        Parameters
        ----------
        name : str
            Genome / species name

        genome_dir : str
            Directory to install genome

        localname : str , optional
            Custom name for your genome

        mask: str , optional
            Masking, soft, hard or none (all other strings)

        regex : str , optional
            Regular expression to select specific chromosome / scaffold names.

        invert_match : bool , optional
            Set to True to select all chromosomes that don't match the regex.

        bgzip : bool , optional
            If set to True the genome FASTA file will be compressed using bgzip.
            If not specified, the setting from the configuration file will be used.
        """
        genome_dir = os.path.expanduser(genome_dir)
        if not os.path.exists(genome_dir):
            os.makedirs(genome_dir)

        dbname, link = self.get_genome_download_link(name, mask=mask, **kwargs)
        myname = get_localname(dbname, localname)
        if not os.path.exists(os.path.join(genome_dir, myname)):
            os.makedirs(os.path.join(genome_dir, myname))

        sys.stderr.write("Downloading genome from {}...\n".format(link))

        # download to tmp dir. Move genome on completion.
        # tmp dir is in genome_dir to prevent moving the genome between disks
        with TemporaryDirectory(dir=os.path.join(genome_dir, myname)) as tmpdir:
            fname = os.path.join(tmpdir, myname + ".fa")

            # actual download
            urlcleanup()
            with urlopen(link) as response:
                # check available memory vs file size.
                available_memory = int(virtual_memory().available)
                file_size = int(response.info()["Content-Length"])
                # download file in chunks if >75% of memory would be used
                cutoff = int(available_memory * 0.75)
                chunk_size = None if file_size < cutoff else cutoff
                with open(fname, "wb") as f_out:
                    shutil.copyfileobj(response, f_out, chunk_size)

            # unzip genome
            if link.endswith("tar.gz"):
                self.tar_to_bigfile(fname, fname)
            elif link.endswith(".gz"):
                # gunzip will only work with files ending with ".gz"
                os.rename(fname, fname + ".gz")
                ret = sp.check_call(["gunzip", "-f", fname])
                if ret != 0:
                    raise Exception("Error gunzipping genome {}".format(fname))

            # process genome (e.g. masking)
            if hasattr(self, "_post_process_download"):
                self._post_process_download(name, localname, tmpdir, mask)

            if regex:
                os.rename(fname, fname + "_to_regex")
                infa = fname + "_to_regex"
                outfa = fname
                filter_fasta(infa, outfa, regex=regex, v=invert_match, force=True)

                not_included = [
                    k for k in Fasta(infa).keys() if k not in Fasta(outfa).keys()
                ]

            # bgzip genome if requested
            if bgzip is None:
                bgzip = config.get("bgzip", False)

            if bgzip:
                ret = sp.check_call(["bgzip", "-f", fname])
                if ret != 0:
                    raise Exception(
                        "Error bgzipping {}. ".format(fname) + "Is tabix installed?"
                    )
                fname += ".gz"

            # transfer the genome from the tmpdir to the genome_dir
            src = fname
            dst = os.path.join(genome_dir, myname, os.path.basename(fname))
            shutil.move(src, dst)

        sys.stderr.write("name: {}\n".format(dbname))
        sys.stderr.write("local name: {}\n".format(myname))
        sys.stderr.write("fasta: {}\n".format(dst))

        # Create readme with information
        readme = os.path.join(genome_dir, myname, "README.txt")
        with open(readme, "w") as f:
            f.write("name: {}\n".format(myname))
            f.write("provider: {}\n".format(self.name))
            f.write("original name: {}\n".format(dbname))
            f.write("original filename: {}\n".format(os.path.split(link)[-1]))
            if hasattr(self, "assembly_accession"):
                f.write(
                    "assembly_accession: {}\n".format(self.assembly_accession(dbname))
                )
            if hasattr(self, "genome_taxid"):
                f.write("taxid: {}\n".format(self.genome_taxid(dbname)))
            f.write("url: {}\n".format(link))
            f.write("mask: {}\n".format(mask))
            f.write("date: {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
            if regex:
                if invert_match:
                    f.write("regex: {} (inverted match)\n".format(regex))
                else:
                    f.write("regex: {}\n".format(regex))
                f.write("sequences that were excluded:\n")
                for seq in not_included:
                    f.write("\t{}\n".format(seq))

    def download_annotation(self, name, genome_dir, localname=None, **kwargs):
        """
        Download annotation file to to a specific directory

        Parameters
        ----------
        name : str
            Genome / species name

        genome_dir : str
            Directory to install annotation

        localname : str , optional
            Custom name for your genome
        """
        raise NotImplementedError()

    def get_genome_download_link(self, name, mask="soft", **kwargs):
        raise NotImplementedError()


register_provider = ProviderBase.register_provider


@register_provider("Ensembl")
class EnsemblProvider(ProviderBase):

    """
    Ensembl genome provider.

    Will search both ensembl.org as well as ensemblgenomes.org.
    The bacteria division is not yet supported.
    """

    rest_url = "http://rest.ensembl.org/"

    def __init__(self):
        # Necessary for bucketcache, otherwise methods will identical names
        # from different classes will use the same cache :-O!
        self.name = "Ensembl"
        self.genomes = None
        self.list_available_genomes()
        self.version = None

    @cached(method=True)
    def request_json(self, ext):
        """Make a REST request and return as json."""
        if self.rest_url.endswith("/") and ext.startswith("/"):
            ext = ext[1:]

        r = requests.get(
            self.rest_url + ext, headers={"Content-Type": "application/json"}
        )

        if not r.ok:
            r.raise_for_status()

        return r.json()

    def list_install_options(self, name=None):
        """List Ensembl specific install options"""

        provider_specific_options = {
            "toplevel": {
                "long": "toplevel",
                "help": "always download toplevel-genome",
                "flag_value": True,
            },
            "version": {
                "long": "version",
                "help": "select release version",
                "type": int,
                "default": None,
            },
        }

        return provider_specific_options

    def list_available_genomes(self, as_dict=False):
        """
        List all available genomes.

        Parameters
        ----------
        as_dict : bool, optional
            Return a dictionary of results.

        Yields
        ------
        genomes : dictionary or tuple
        """
        if self.genomes is None or len(self.genomes) == 0:
            self.genomes = []
            divisions = self.request_json("info/divisions?")
            for division in divisions:
                if division == "EnsemblBacteria":
                    continue
                genomes = self.request_json(
                    "info/genomes/division/{}?".format(division)
                )
                self.genomes += genomes

        for genome in self.genomes:
            if as_dict:
                yield genome
            else:
                yield (
                    self.safe(genome.get("assembly_name", "")),
                    genome.get("name", ""),
                )

    def _get_genome_info(self, name):
        """Get genome_info from json request."""
        try:
            assembly_acc = ""
            for genome in self.list_available_genomes(as_dict=True):
                if self.safe(genome.get("assembly_name", "")) == self.safe(name):
                    assembly_acc = genome.get("assembly_accession", "na")
                    break
            if assembly_acc:
                ext = "info/genomes/assembly/" + assembly_acc + "/?"
                genome_info = self.request_json(ext)
            else:
                raise exceptions.GenomeDownloadError(
                    "Could not download genome {} from Ensembl".format(name)
                )
        except requests.exceptions.HTTPError as e:
            sys.stderr.write("Species not found: {}".format(e))
            raise exceptions.GenomeDownloadError(
                "Could not download genome {} from Ensembl".format(name)
            )
        return genome_info

    # Doesn't need to be cached, quick enough
    def assembly_accession(self, name):
        """Return the assembly accession (GCA_*) for a genome.

        Parameters
        ----------
        name : str
            Genome name.

        Yields
        ------
        str
            Assembly accession.
        """
        genome_info = self._get_genome_info(name)
        return genome_info.get("assembly_accession", "unknown")

    # Doesn't need to be cached, quick enough
    def genome_taxid(self, name):
        """Return the taxonomy_id for a genome.

        Parameters
        ----------
        name : str
            Assembly name.

        Yields
        ------
        int
            Taxonomy id.
        """
        genome_info = self._get_genome_info(name)
        return genome_info.get("taxonomy_id", -1)

    def _genome_info_tuple(self, genome):
        return (
            self.safe(genome.get("assembly_name", "")),
            genome.get("assembly_accession", "na"),
            genome.get("scientific_name", ""),
            str(genome.get("taxonomy_id", "")),
            genome.get("genebuild", ""),
        )

    def search(self, term):
        """
        Search for a genome at Ensembl.

        Both the name and description are used for the
        search. Search term is case-insensitive.

        Parameters
        ----------
        term : str
            Search term, case-insensitive.

        Yields
        ------
        tuple
            genome information (name/identifier and description)
        """
        taxid = False
        try:
            int(term)
            taxid = True
        except ValueError:
            term = term.lower()

        for genome in self.list_available_genomes(as_dict=True):
            if taxid:
                if str(term) == str(genome.get("taxonomy_id", "")):
                    yield self._genome_info_tuple(genome)
            elif term in ",".join([str(v) for v in genome.values()]).lower():
                yield self._genome_info_tuple(genome)

    def get_version(self, ftp_site):
        """Retrieve current version from Ensembl FTP.
        """
        print("README", ftp_site)
        with urlopen(ftp_site + "/current_README") as response:
            p = re.compile(r"Ensembl (Genomes|Release) (\d+)")
            m = p.search(response.read().decode())
        if m:
            version = m.group(2)
            sys.stderr.write("Using version {}\n".format(version))
            self.version = version
            return version

    def get_genome_download_link(self, name, mask="soft", **kwargs):
        """
        Return Ensembl ftp link to the genome sequence

        Parameters
        ----------
        name : str
            Genome name. Current implementation will fail if exact
            name is not found.

        mask : str , optional
            Masking level. Options: soft, hard or none. Defaults to soft.

        Returns
        ------
        tuple (name, link) where name is the Ensembl dbname identifier
        and link is a str with the ftp download link.
        """
        genome_info = self._get_genome_info(name)

        # parse the division
        division = genome_info["division"].lower().replace("ensembl", "")
        if division == "bacteria":
            raise NotImplementedError("bacteria from ensembl not yet supported")

        ftp_site = "ftp://ftp.ensemblgenomes.org/pub"
        if division == "vertebrates":
            ftp_site = "http://ftp.ensembl.org/pub"

        version = self.version
        if kwargs.get("version", None):
            version = kwargs.get("version")
        elif not version:
            version = self.get_version(ftp_site)

        if division != "vertebrates":
            base_url = "/{}/release-{}/fasta/{}/dna/"
            ftp_dir = base_url.format(
                division, version, genome_info["url_name"].lower()
            )
            url = "{}/{}".format(ftp_site, ftp_dir)
        else:
            base_url = "/release-{}/fasta/{}/dna/"
            ftp_dir = base_url.format(version, genome_info["url_name"].lower())
            url = "{}/{}".format(ftp_site, ftp_dir)

        def get_url(level="toplevel"):
            pattern = "dna.{}".format(level)
            if mask == "soft":
                pattern = "dna_sm.{}".format(level)
            elif mask == "hard":
                pattern = "dna_rm.{}".format(level)

            _asm_url = "{}/{}.{}.{}.fa.gz".format(
                url,
                genome_info["url_name"].capitalize(),
                re.sub(r"\.p\d+$", "", self.safe(genome_info["assembly_name"])),
                pattern,
            )
            return _asm_url

        # first try the (much smaller) primary assembly, otherwise use the toplevel assembly
        if kwargs.get("toplevel", False):
            sys.stderr.write("skipping primary assembly check\n")
            asm_url = get_url()
        else:
            try:
                asm_url = get_url("primary_assembly")
                with urlopen(asm_url):
                    pass
            except URLError:
                asm_url = get_url()

        return self.safe(genome_info["assembly_name"]), asm_url

    def download_annotation(self, name, genome_dir, localname=None, **kwargs):
        """
        Download Ensembl annotation file to to a specific directory

        Parameters
        ----------
        name : str
            Ensembl genome name.
        genome_dir : str
            Genome directory.
        localname : str , optional
            Alternative name for the genome
        kwargs: dict , optional:
            Provider specific options.

            version : int , optional
                Ensembl version. By default the latest version is used.
        """
        sys.stderr.write("Downloading gene annotation...\n")

        localname = get_localname(name, localname)
        genome_info = self._get_genome_info(name)

        # parse the division
        division = genome_info["division"].lower().replace("ensembl", "")
        if division == "bacteria":
            raise NotImplementedError("bacteria from ensembl not yet supported")

        # Get the base link depending on division
        ftp_site = "ftp://ftp.ensemblgenomes.org/pub"
        if division == "vertebrates":
            ftp_site = "http://ftp.ensembl.org/pub"

        version = self.version
        if kwargs.get("version", None):
            version = kwargs.get("version")
        elif not version:
            version = self.get_version(ftp_site)

        if division != "vertebrates":
            ftp_site += "/{}".format(division)

        # Get the GTF URL
        base_url = ftp_site + "/release-{}/gtf/{}/{}.{}.{}.gtf.gz"
        safe_name = name.replace(" ", "_")
        safe_name = re.sub(r"\.p\d+$", "", safe_name)

        annot_url = base_url.format(
            version,
            genome_info["url_name"].lower(),
            genome_info["url_name"].capitalize(),
            safe_name,
            version,
        )

        attempt_download_and_report_back(
            genome_dir=genome_dir, annot_url=annot_url, localname=localname
        )


@register_provider("UCSC")
class UcscProvider(ProviderBase):

    """
    UCSC genome provider.

    The UCSC API REST server is used to search and list genomes.
    """

    base_url = "http://hgdownload.soe.ucsc.edu/goldenPath"
    ucsc_url = base_url + "/{0}/bigZips/chromFa.tar.gz"
    ucsc_url_masked = base_url + "/{0}/bigZips/chromFaMasked.tar.gz"
    alt_ucsc_url = base_url + "/{0}/bigZips/{0}.fa.gz"
    alt_ucsc_url_masked = base_url + "/{0}/bigZips/{0}.fa.masked.gz"
    rest_url = "http://api.genome.ucsc.edu/list/ucscGenomes"

    def __init__(self):
        # Necessary for bucketcache, otherwise methods will identical names
        # from different classes will use the same cache :-O!
        self.name = "UCSC"
        # Populate on init, so that methods can be cached
        self.genomes = self._get_genomes()

    def list_available_genomes(self):
        """
        List all available genomes.

        Returns
        -------
        genomes : list
        """
        self.genomes = self._get_genomes()

        return self.genomes

    @cached(method=True)
    def _get_genomes(self):
        r = requests.get(self.rest_url, headers={"Content-Type": "application/json"})
        if not r.ok:
            r.raise_for_status()
        ucsc_json = r.json()
        genomes = ucsc_json["ucscGenomes"]
        return genomes

    @cached(method=True)
    def assembly_accession(self, genome_build):
        """Return the assembly accession (GCA_*) for a genome.

        UCSC does not server the assembly accession through the REST API.
        Therefore, the readme.html is scanned for a GCA assembly id. If it is
        not found, the linked NCBI assembly page will be checked. Especially
        for older genome builds, the GCA will not be present, in which case
        "na" will be returned.

        Parameters
        ----------
        genome_build : str
            UCSC genome build name.

        Yields
        ------
        str
            Assembly accession.
        """
        ucsc_url = (
            "https://hgdownload.soe.ucsc.edu/"
            + self._get_genomes()[genome_build]["htmlPath"]
        )
        print(ucsc_url)
        p = re.compile(r"GCA_\d+\.\d+")
        p_ncbi = re.compile(r"https?://www.ncbi.nlm.nih.gov/assembly/\d+")
        text = read_url(ucsc_url)
        m = p.search(text)
        # Default, if not found. This matches NCBI, which will also return na.
        gca = "na"
        if m:
            # Get the GCA from the html
            gca = m.group(0)
        else:
            # Search for an assembly link at NCBI
            m = p_ncbi.search(text)
            if m:
                ncbi_url = m.group(0)
                text = read_url(ncbi_url)
                # We need to select the line that contains the assembly accession.
                # The page will potentially contain many more links to newer assemblies
                lines = text.split("\n")
                text = "\n".join(
                    [line for line in lines if "RefSeq assembly accession:" in line]
                )
                m = p.search(text)
                if m:
                    gca = m.group(0)
        return gca

    @cached(method=True)
    def genome_taxid(self, genome_build):
        """Return the taxonomy_id for a genome.

        Parameters
        ----------
        genome_build : str
            UCSC genome build name.

        Yields
        ------
        int
            Taxonomy id..
        """
        return self._get_genomes()[genome_build]["taxId"]

    def _genome_info_tuple(self, name):
        genome = self.list_available_genomes()[name]
        return (
            name,
            str(self.assembly_accession(name)),
            genome["scientificName"],
            str(genome["taxId"]),
            genome["description"],
        )

    def search(self, term):
        """
        Search for a genome at UCSC.

        Both the name and description are used for the
        search. Search term is case-insensitive.

        Parameters
        ----------
        term : str
            Search term, case-insensitive. Can be genome build id (mm10, hg38),
            scientific name or taxonomy id.

        Yields
        ------
        tuple
            genome information (name/identifier and description)
        """
        taxid = False
        try:
            term = int(term)
            taxid = True
        except ValueError:
            term = term.lower().replace(" ", "_")
            pass

        genomes = self.list_available_genomes()
        if taxid:
            for name in genomes:
                genome = genomes[name]
                if term == genome["taxId"]:
                    yield self._genome_info_tuple(name)
        elif term in genomes:
            yield self._genome_info_tuple(term)
        else:
            for name in genomes:
                genome = genomes[name]
                for field in ["description", "scientificName"]:
                    if term in genome[field].lower().replace(" ", "_"):
                        yield self._genome_info_tuple(name)

    def get_genome_download_link(self, name, mask="soft", **kwargs):
        """
        Return UCSC http link to genome sequence

        Parameters
        ----------
        name : str
            Genome build name. Current implementation will fail if exact
            name is not found.

        mask : str , optional
            Masking level. Options: soft, hard or none. Defaults to soft.

        Returns
        ------
        tuple (name, link) where name is the genome build identifier
        and link is a str with the http download link.
        """
        if mask not in ["soft", "hard"]:
            sys.stderr.write("ignoring mask parameter for UCSC at download.\n")

        urls = [self.ucsc_url, self.alt_ucsc_url]
        if mask == "hard":
            urls = [self.ucsc_url_masked, self.alt_ucsc_url_masked]

        for genome_url in urls:
            remote = genome_url.format(name)
            ret = requests.head(remote)

            if ret.status_code == 200:
                return name, remote

        raise exceptions.GenomeDownloadError(
            "Could not download genome {} from UCSC".format(name)
        )

    @staticmethod
    def _post_process_download(name, localname, out_dir, mask="soft"):
        """
        Unmask a softmasked genome if required

        Parameters
        ----------
        name : str
            UCSC genome name

        out_dir : str
            Output directory
        """
        if mask not in ["hard", "soft"]:
            localname = get_localname(name, localname)

            # Check of the original genome fasta exists
            fa = os.path.join(out_dir, "{}.fa".format(localname))
            if not os.path.exists(fa):
                raise Exception("Genome fasta file not found, {}".format(fa))

            sys.stderr.write("UCSC genomes are softmasked by default. Unmasking...\n")

            # write in a tmp file
            new_fa = os.path.join(
                out_dir, localname, ".process.{}.fa".format(localname)
            )
            with open(fa) as old:
                with open(new_fa, "w") as new:
                    for line in old:
                        if not line.startswith(">"):
                            new.write(line.upper())
                        else:
                            new.write(line)

            # overwrite original file with tmp file
            shutil.move(new_fa, fa)

    def download_annotation(self, name, genome_dir, localname=None, **kwargs):
        """
        Download UCSC annotation file to to a specific directory.

        Will check UCSC, Ensembl and RefSeq annotation.

        Parameters
        ----------
        name : str
            UCSC genome name.
        genome_dir : str
            Genome directory.
        localname : str , optional
            Custom name for your genome
        """
        sys.stderr.write("Downloading gene annotation...\n")

        localname = get_localname(name, localname)

        ucsc_gene_url = "http://hgdownload.cse.ucsc.edu/goldenPath/{}/database/"
        annos = ["knownGene.txt.gz", "ensGene.txt.gz", "refGene.txt.gz"]

        anno = []
        p = re.compile(r"\w+.Gene.txt.gz")
        with urlopen(ucsc_gene_url.format(name)) as f:
            for line in f.readlines():
                m = p.search(line.decode())
                if m:
                    anno.append(m.group(0))

        annot_url = ""
        for a in annos:
            if a in anno:
                annot_url = ucsc_gene_url.format(name) + a
                break

        attempt_download_and_report_back(
            genome_dir=genome_dir, annot_url=annot_url, localname=localname
        )


@register_provider("NCBI")
class NcbiProvider(ProviderBase):

    """
    NCBI genome provider.

    Uses the assembly reports page to search and list genomes.
    """

    assembly_url = "https://ftp.ncbi.nlm.nih.gov/genomes/ASSEMBLY_REPORTS/"

    def __init__(self):
        # Necessary for bucketcache, otherwise methods will identical names
        # from different classes will use the same cache :-O!
        self.name = "NCBI"
        self.genomes = self._get_genomes()

    @cached(method=True)
    def _get_genomes(self):
        """Parse genomes from assembly summary txt files."""
        genomes = []

        names = [
            "assembly_summary_refseq.txt",
            "assembly_summary_genbank.txt",
            "assembly_summary_refseq_historical.txt",
        ]

        sys.stderr.write(
            "Downloading assembly summaries from NCBI, " + "this will take a while...\n"
        )
        seen = {}
        for fname in names:
            urlcleanup()
            with urlopen(os.path.join(self.assembly_url, fname)) as response:
                lines = response.read().decode("utf-8").splitlines()
            header = lines[1].strip("# ").split("\t")
            for line in lines[2:]:
                vals = line.strip("# ").split("\t")
                # Don't repeat samples with the same asn_name
                if vals[15] not in seen:  # asn_name
                    genomes.append(dict(zip(header, vals)))
                    seen[vals[15]] = 1

        return genomes

    @staticmethod
    def _genome_info_tuple(genome):
        # Consistency! This way we always either get a GCA accession or na
        accessions = [
            genome.get(col) for col in ["gbrs_paired_asm", "assembly_accession"]
        ]
        for accession in accessions:
            if accession.startswith("GCA"):
                break
        else:
            accession = "na"

        return (
            genome.get("asm_name", ""),
            accession,
            genome.get("organism_name", ""),
            str(genome.get("species_taxid", "")),
            genome.get("submitter", ""),
        )

    def list_available_genomes(self, as_dict=False):
        """
        List all available genomes.

        Parameters
        ----------
        as_dict : bool, optional
            Return a dictionary of results.

        Yields
        ------
        genomes : dictionary or tuple
        """
        if not self.genomes:
            self.genomes = self._get_genomes()

        for genome in self.genomes:
            if as_dict:
                yield genome
            else:
                yield self._genome_info_tuple(genome)

    @cached(method=True)
    def assembly_accession(self, name):
        """Return the assembly accession (GCA_*) for a genome.

        Parameters
        ----------
        name : str
            Genome name.

        Yields
        ------
        str
            Assembly accession.
        """
        for genome in self._get_genomes():
            if name in [genome["asm_name"], genome["asm_name"].replace(" ", "_")]:
                accessions = [
                    genome.get(col) for col in ["gbrs_paired_asm", "assembly_accession"]
                ]
                for accession in accessions:
                    if accession.startswith("GCA"):
                        return accession

                return "na"

    @cached(method=True)
    def genome_taxid(self, name):
        """Return the taxonomy_id for a genome.

        Parameters
        ----------
        name : str
            Assembly name.

        Yields
        ------
        int
            Taxonomy id.
        """
        for genome in self._get_genomes():
            if name in [genome["asm_name"], genome["asm_name"].replace(" ", "_")]:
                return genome.get("species_taxid", "na")

    def search(self, term):
        """
        Search for term in genome names and descriptions of NCBI.

        The search is case-insensitive.

        Parameters
        ----------
        term : str
            search term

        Yields
        ------
        tuples with two items, name and description
        """
        taxid = False
        try:
            int(term)
            taxid = True
        except ValueError:
            term = term.lower().replace(" ", "_")

        for genome in self.list_available_genomes(as_dict=True):
            if taxid:
                term_str = str(genome.get("species_taxid", ""))
            else:
                term_str = ";".join(
                    [repr(x).replace(" ", "_") for x in genome.values()]
                )

            if (taxid and term == term_str) or (not taxid and term in term_str.lower()):
                yield self._genome_info_tuple(genome)

    def get_genome_download_link(self, name, mask="soft", **kwargs):
        """
        Return NCBI ftp link to top-level genome sequence

        Parameters
        ----------
        name : str
            Genome name. Current implementation will fail if exact
            name is not found.

        mask : str , optional
            Masking level. Options: soft, hard or none. Defaults to soft.

        Returns
        ------
        tuple (name, link) where name is the NCBI asm_name identifier
        and link is a str with the ftp download link.
        """
        if mask != "soft":
            sys.stderr.write("ignoring mask parameter for NCBI at download.\n")

        if not self.genomes:
            self.genomes = self._get_genomes()

        for genome in self.genomes:
            if name in [genome["asm_name"], genome["asm_name"].replace(" ", "_")]:
                url = genome["ftp_path"]
                url = url.replace("ftp://", "https://")
                url += "/" + url.split("/")[-1] + "_genomic.fna.gz"
                return name, url
        raise exceptions.GenomeDownloadError("Could not download genome from NCBI")

    def _post_process_download(self, name, localname, out_dir, mask="soft"):
        """
        Replace accessions with sequence names in fasta file.

        Parameters
        ----------
        name : str
            NCBI genome name

        out_dir : str
            Output directory
        """
        # Get the FTP url for this specific genome and download
        # the assembly report
        for genome in self.genomes:
            if name in [genome["asm_name"], genome["asm_name"].replace(" ", "_")]:
                url = genome["ftp_path"]
                url += "/" + url.split("/")[-1] + "_assembly_report.txt"
                url = url.replace("ftp://", "https://")
                break
        else:
            raise Exception("Genome {} not found in NCBI genomes".format(name))

        # Create mapping of accessions to names
        tr = {}
        urlcleanup()
        with urlopen(url) as response:
            for line in response.read().decode("utf-8").splitlines():
                if line.startswith("#"):
                    continue
                vals = line.strip().split("\t")
                tr[vals[6]] = vals[0]

        localname = get_localname(name, localname)
        # Check of the original genome fasta exists
        fa = os.path.join(out_dir, "{}.fa".format(localname))
        if not os.path.exists(fa):
            raise Exception("Genome fasta file not found, {}".format(fa))

        # Use a tmp file and replace the names
        new_fa = os.path.join(out_dir, ".process.{}.fa".format(localname))
        if mask != "soft":
            sys.stderr.write(
                "NCBI genomes are softmasked by default. Changing mask...\n"
            )

        with open(fa) as old:
            with open(new_fa, "w") as new:
                for line in old:
                    if line.startswith(">"):
                        desc = line.strip()[1:]
                        name = desc.split(" ")[0]
                        new.write(">{} {}\n".format(tr.get(name, name), desc))
                    elif mask == "hard":
                        new.write(re.sub("[actg]", "N", line))
                    elif mask not in ["hard", "soft"]:
                        new.write(line.upper())
                    else:
                        new.write(line)

        # Rename tmp file to real genome file
        shutil.move(new_fa, fa)

    def download_annotation(self, name, genome_dir, localname=None, **kwargs):
        """
        Download NCBI annotation file to to a specific directory

        Parameters
        ----------
        name : str
            Genome / species name

        genome_dir : str
            Directory to install annotation

        localname : str , optional
            Custom name for your genome
        """
        sys.stderr.write("Downloading gene annotation...\n")

        localname = get_localname(name, localname)
        if not self.genomes:
            self.genomes = self._get_genomes()

        for genome in self.genomes:
            if name in [genome["asm_name"], genome["asm_name"].replace(" ", "_")]:
                annot_url = genome["ftp_path"]
                annot_url = annot_url.replace("ftp://", "https://")
                annot_url += "/" + annot_url.split("/")[-1] + "_genomic.gff.gz"
                break
        else:
            raise Exception("Genome {} not found in NCBI genomes".format(name))

        attempt_download_and_report_back(
            genome_dir=genome_dir, annot_url=annot_url, localname=localname
        )


@register_provider("URL")
class UrlProvider(ProviderBase):
    """
    URL genome provider.

    Simply download a genome directly through an url.
    """

    def get_genome_download_link(self, url, mask=None, **kwargs):
        """
        url : str
            url of where to download genome from

        mask : str , optional
            Masking level. Not available for URL

        Returns
        ------
        tuple (url, url)
        """
        return url, url

    def list_install_options(self, name=None):
        """List URL specific install options"""

        provider_specific_options = {
            "to_annotation": {
                "long": "to-annotation",
                "help": "link to the annotation file, required if this is not in the same directory as the fasta file",
                "default": None,
            },
        }

        return provider_specific_options

    def download_annotation(self, url, genome_dir, localname=None, **kwargs):
        """
        Attempts to download a gtf or gff3 file from the same location as the genome url

        Parameters
        ----------
        url : str
            url of where to download genome from

        genome_dir : str
            Directory to install annotation

        localname : str , optional
            Custom name for your genome

        kwargs: dict , optional:
            Provider specific options.

            to_annotation : str , optional
                url to annotation file (only required if this not located in the same directory as the fasta)
        """
        # to download the file we need annot_url, ext and gz
        # if a direct annotation URL is provided, this can be parsed
        # else we search the fasta directory
        direct_link = kwargs.get("to_annotation", None)
        if direct_link:
            sys.stderr.write("Downloading annotation from provided url\n")

            # set variables for downloading
            annot_url = direct_link
            ext = get_file_info(annot_url)[0]
            if ext not in [".gtf", ".gff", ".gff3", ".bed"]:
                sys.stderr.write(
                    "WARNING: Only (gzipped) gtf, gff and bed files are currently supported. Skipping\n"
                )
                return

        else:
            urldir = os.path.dirname(url)
            sys.stderr.write(
                "You have requested gene annotation to be downloaded.\n"
                "Genomepy will check the remote directory:\n"
                "{} for annotation files...\n".format(urldir)
            )

            # try to find a GTF or GFF3 file
            name = get_localname(url)
            with urlopen(urldir) as f:
                for urlline in f.readlines():
                    urlstr = str(urlline)
                    if any(
                        substring in urlstr.lower()
                        for substring in [".gtf", name + ".gff"]
                    ):
                        break

            # retrieve the filename from the HTML line
            for split in re.split('>|<|><|/|"', urlstr):
                if split.lower().endswith(
                    (
                        ".gtf",
                        ".gtf.gz",
                        name + ".gff",
                        name + ".gff.gz",
                        name + ".gff3",
                        name + ".gff3.gz",
                    )
                ):
                    fname = split
                    break
            else:
                sys.stderr.write(
                    "WARNING: Could not find gene annotation file, skipping.\n"
                )
                return

            # set variables for downloading
            annot_url = urldir + "/" + fname

        name = get_localname(url)
        localname = get_localname(name, localname)
        attempt_download_and_report_back(
            genome_dir=genome_dir, annot_url=annot_url, localname=localname
        )
