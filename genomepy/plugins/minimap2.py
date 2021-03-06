import os
from shutil import rmtree
from genomepy.plugin import Plugin
from genomepy.utils import mkdir_p, cmd_ok, run_index_cmd


class Minimap2Plugin(Plugin):
    def after_genome_download(self, genome, force=False):
        if not cmd_ok("minimap2"):
            return

        # Create index dir
        index_dir = genome.props["minimap2"]["index_dir"]
        index_name = genome.props["minimap2"]["index_name"]
        if force:
            # Start from scratch
            rmtree(index_dir, ignore_errors=True)
        mkdir_p(index_dir)

        if not any(fname.endswith(".mmi") for fname in os.listdir(index_dir)):
            # Create index
            cmd = "minimap2 -d {} {}".format(index_name, genome.filename)
            run_index_cmd("minimap2", cmd)

    def get_properties(self, genome):
        props = {
            "index_dir": os.path.join(
                os.path.dirname(genome.filename), "index", "minimap2"
            ),
            "index_name": os.path.join(
                os.path.dirname(genome.filename),
                "index",
                "minimap2",
                "{}.mmi".format(genome.name),
            ),
        }
        return props
