import yaml
import json
import time
import random
import string
import logging
import os
from fabric.api import prefix, local, run, env, lcd, parallel # type: ignore
from fabric.contrib.console import confirm # type: ignore
from fabric.contrib.project import rsync_project # type: ignore

from awstools.afitools import *
from awstools.awstools import send_firesim_notification
from util.streamlogger import StreamLogger, InfoStreamLogger

from typing import Optional, TYPE_CHECKING
# TODO: Solved by "from __future__ import annotations" (see https://stackoverflow.com/questions/33837918/type-hints-solve-circular-dependency)
if TYPE_CHECKING:
    from buildtools.buildconfig import BuildConfig
else:
    BuildConfig = object

rootLogger = logging.getLogger()

def get_deploy_dir() -> str:
    """Determine where the firesim/deploy directory is and return its path.

    Returns:
        Path to firesim/deploy directory.
    """
    with StreamLogger('stdout'), StreamLogger('stderr'):
        deploydir = local("pwd", capture=True)
    return deploydir

class BitBuilder:
    """Abstract class to manage how to build a bitstream for a build config.

    Attributes:
        build_config: Build config to build a bitstream for.
    """
    build_config: BuildConfig

    def __init__(self, build_config: BuildConfig) -> None:
        """
        Args:
            build_config: Build config to build a bitstream for.
        """
        self.build_config = build_config

    def replace_rtl(self) -> None:
        """Generate Verilog from build config."""
        raise NotImplementedError

    def build_driver(self) -> None:
        """Build FireSim FPGA driver from build config."""
        raise NotImplementedError

    def build_bitstream(self, bypass: bool = False) -> None:
        """Run bitstream build and terminate the build host at the end.
        Must run after `replace_rtl` and `build_driver` are run.

        Args:
            bypass: If true, immediately return and terminate build host. Used for testing purposes.
        """
        raise NotImplementedError

class F1BitBuilder(BitBuilder):
    """Bit builder class that builds a AWS EC2 F1 AGFI (bitstream) from the build config."""
    def replace_rtl(self) -> None:
        """Generate Verilog from build config."""
        rootLogger.info("Building Verilog for {}".format(str(self.build_config.get_chisel_triplet())))

        with prefix(f'cd {get_deploy_dir()}/../'), \
            prefix(f'export RISCV={os.getenv("RISCV", "")}'), \
            prefix(f'export PATH={os.getenv("PATH", "")}'), \
            prefix(f'export LD_LIBRARY_PATH={os.getenv("LD_LIBRARY_PATH", "")}'), \
            prefix('source sourceme-f1-manager.sh'), \
            prefix('cd sim/'), \
            InfoStreamLogger('stdout'), \
            InfoStreamLogger('stderr'):
            run(self.build_config.make_recipe("PLATFORM=f1 replace-rtl"))

    def build_driver(self) -> None:
        """Build FireSim FPGA driver from build config."""
        rootLogger.info("Building FPGA driver for {}".format(str(self.build_config.get_chisel_triplet())))

        with prefix(f'cd {get_deploy_dir()}/../'), \
            prefix(f'export RISCV={os.getenv("RISCV", "")}'), \
            prefix(f'export PATH={os.getenv("PATH", "")}'), \
            prefix(f'export LD_LIBRARY_PATH={os.getenv("LD_LIBRARY_PATH", "")}'), \
            prefix('source sourceme-f1-manager.sh'), \
            prefix('cd sim/'), \
            InfoStreamLogger('stdout'), \
            InfoStreamLogger('stderr'):
            run(self.build_config.make_recipe("PLATFORM=f1 driver"))

    def cl_dir_setup(chisel_triplet: str, dest_build_dir: str) -> str:
        """Setup CL_DIR on build host.

        Args:
            chisel_triplet: Build config chisel triplet used to uniquely identify build dir.
            dest_build_dir: Destination base directory to use.

        Returns:
            Path to CL_DIR directory (that is setup) or `None` if invalid.
        """
        fpga_build_postfix = f"hdk/cl/developer_designs/cl_{chisel_triplet}"

        # local paths
        local_awsfpga_dir = f"{get_deploy_dir()}/../platforms/f1/aws-fpga"

        dest_f1_platform_dir = f"{dest_build_dir}/platforms/f1/"
        dest_awsfpga_dir = f"{dest_f1_platform_dir}/aws-fpga"

        # copy aws-fpga to the build instance.
        # do the rsync, but ignore any checkpoints that might exist on this machine
        # (in case builds were run locally)
        # extra_opts -l preserves symlinks
        with StreamLogger('stdout'), StreamLogger('stderr'):
            run(f'mkdir -p {dest_f1_platform_dir}')
            rsync_cap = rsync_project(
            local_dir=local_awsfpga_dir,
            remote_dir=dest_f1_platform_dir,
            ssh_opts="-o StrictHostKeyChecking=no",
            exclude=["hdk/cl/developer_designs/cl_*"],
            extra_opts="-l", capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)
            rsync_cap = rsync_project(
            local_dir=f"{local_awsfpga_dir}/{fpga_build_postfix}/*",
            remote_dir=f'{dest_awsfpga_dir}/{fpga_build_postfix}',
            exclude=["build/checkpoints"],
            ssh_opts="-o StrictHostKeyChecking=no",
            extra_opts="-l", capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)

        return f"{dest_awsfpga_dir}/{fpga_build_postfix}"

    def build_bitstream(self, bypass: bool = False) -> None:
        """Run Vivado, convert tar -> AGFI/AFI, and then terminate the instance at the end.

        Args:
            bypass: If true, immediately return and terminate build host. Used for testing purposes.
        """
        if bypass:
            self.build_config.build_farm_host_dispatcher.release_build_farm_host()
            return

        # The default error-handling procedure. Send an email and teardown instance
        def on_build_failure():
            """ Terminate build host and notify user that build failed """

            message_title = "FireSim FPGA Build Failed"

            message_body = "Your FPGA build failed for triplet: " + self.build_config.get_chisel_triplet()

            send_firesim_notification(message_title, message_body)

            rootLogger.info(message_title)
            rootLogger.info(message_body)

            self.build_config.build_host_dispatcher.release_build_host()

        rootLogger.info("Building AWS F1 AGFI from Verilog")

        local_deploy_dir = get_deploy_dir()
        fpga_build_postfix = "hdk/cl/developer_designs/cl_{}".format(self.build_config.get_chisel_triplet())
        local_results_dir = "{}/results-build/{}".format(local_deploy_dir, self.build_config.get_build_dir_name())

        # cl_dir is the cl_dir that is either local or remote
        # if locally no need to copy things around (the makefile should have already created a CL_DIR w. the tuple)
        # if remote (aka not locally) then you need to copy things
        cl_dir = ""
        local_cl_dir = "{}/../platforms/f1/aws-fpga/{}".format(local_deploy_dir, fpga_build_postfix)

        # copy over generated RTL into local CL_DIR before remote
        with InfoStreamLogger('stdout'), InfoStreamLogger('stderr'):
            run("""mkdir -p {}""".format(local_results_dir))
            run("""cp {}/design/FireSim-generated.sv {}/FireSim-generated.sv""".format(local_cl_dir, local_results_dir))

        if self.build_config.build_farm_host_dispatcher.is_local:
            cl_dir = local_cl_dir
        else:
            cl_dir = self.remote_setup()

        vivado_result = 0
        with InfoStreamLogger('stdout'), InfoStreamLogger('stderr'):
            # copy script to the cl_dir and execute
            rsync_cap = rsync_project(
                local_dir="{}/../platforms/f1/build-bitstream.sh".format(local_deploy_dir),
                remote_dir="{}/".format(cl_dir),
                ssh_opts="-o StrictHostKeyChecking=no",
                extra_opts="-l", capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)

            vivado_result = run("{}/build-bitstream.sh {}".format(cl_dir, cl_dir)).return_code

        # put build results in the result-build area
        with StreamLogger('stdout'), StreamLogger('stderr'):
            rsync_cap = rsync_project(
                local_dir="{}/".format(local_results_dir),
                remote_dir="{}".format(cl_dir),
                ssh_opts="-o StrictHostKeyChecking=no", upload=False, extra_opts="-l",
                capture=True)
            rootLogger.debug(rsync_cap)
            rootLogger.debug(rsync_cap.stderr)

        if vivado_result != 0:
            on_build_failure()
            return

        if not self.aws_create_afi():
            on_build_failure()
            return

        self.build_config.build_farm_host_dispatcher.release_build_farm_host()

    def aws_create_afi(build_config: BuildConfig) -> Optional[bool]:
        """Convert the tarball created by Vivado build into an Amazon Global FPGA Image (AGFI).

        Args:
            build_config: Build config to determine paths.

        Returns:
            `True` on success, `None` on error.
        """
        local_deploy_dir = get_deploy_dir()
        local_results_dir = f"{local_deploy_dir}/results-build/{build_config.get_build_dir_name()}"

        afi = None
        agfi = None
        s3bucket = build_config.s3_bucketname
        afiname = build_config.name

        # construct the "tags" we store in the AGFI description
        tag_buildtriplet = build_config.get_chisel_triplet()
        tag_deploytriplet = tag_buildtriplet
        if build_config.deploytriplet:
            tag_deploytriplet = build_config.deploytriplet

        # the asserts are left over from when we tried to do this with tags
        # - technically I don't know how long these descriptions are allowed to be,
        # but it's at least 256*3, so I'll leave these here for now as sanity
        # checks.
        assert len(tag_buildtriplet) <= 255, "ERR: aws does not support tags longer than 256 chars for buildtriplet"
        assert len(tag_deploytriplet) <= 255, "ERR: aws does not support tags longer than 256 chars for deploytriplet"

        with StreamLogger('stdout'), StreamLogger('stderr'):
            is_dirty_str = local("if [[ $(git status --porcelain) ]]; then echo '-dirty'; fi", capture=True)
            hash = local("git rev-parse HEAD", capture=True)
        tag_fsimcommit = hash + is_dirty_str

        assert len(tag_fsimcommit) <= 255, "ERR: aws does not support tags longer than 256 chars for fsimcommit"

        # construct the serialized description from these tags.
        description = firesim_tags_to_description(tag_buildtriplet, tag_deploytriplet, tag_fsimcommit)

        # if we're unlucky, multiple vivado builds may launch at the same time. so we
        # append the build node IP + a random string to diff them in s3
        global_append = "-" + str(env.host_string) + "-" + ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(10)) + ".tar"

        with lcd(f"{local_results_dir}/cl_{tag_buildtriplet}/build/checkpoints/to_aws/"), StreamLogger('stdout'), StreamLogger('stderr'):
            files = local('ls *.tar', capture=True)
            rootLogger.debug(files)
            rootLogger.debug(files.stderr)
            tarfile = files.split()[-1]
            s3_tarfile = tarfile + global_append
            localcap = local('aws s3 cp ' + tarfile + ' s3://' + s3bucket + '/dcp/' + s3_tarfile, capture=True)
            rootLogger.debug(localcap)
            rootLogger.debug(localcap.stderr)
            agfi_afi_ids = local(f"""aws ec2 create-fpga-image --input-storage-location Bucket={s3bucket},Key={"dcp/" + s3_tarfile} --logs-storage-location Bucket={s3bucket},Key={"logs/"} --name "{afiname}" --description "{description}" """, capture=True)
            rootLogger.debug(agfi_afi_ids)
            rootLogger.debug(agfi_afi_ids.stderr)
            rootLogger.debug("create-fpge-image result: " + str(agfi_afi_ids))
            ids_as_dict = json.loads(agfi_afi_ids)
            agfi = ids_as_dict["FpgaImageGlobalId"]
            afi = ids_as_dict["FpgaImageId"]
            rootLogger.info("Resulting AGFI: " + str(agfi))
            rootLogger.info("Resulting AFI: " + str(afi))

        rootLogger.info("Waiting for create-fpga-image completion.")
        checkstate = "pending"
        with lcd(local_results_dir), StreamLogger('stdout'), StreamLogger('stderr'):
            while checkstate == "pending":
            imagestate = local(f"aws ec2 describe-fpga-images --fpga-image-id {afi} | tee AGFI_INFO", capture=True)
            state_as_dict = json.loads(imagestate)
            checkstate = state_as_dict["FpgaImages"][0]["State"]["Code"]
            rootLogger.info("Current state: " + str(checkstate))
            time.sleep(10)


        if checkstate == "available":
            # copy the image to all regions for the current user
            copy_afi_to_all_regions(afi)

            message_title = "FireSim FPGA Build Completed"
            agfi_entry = afiname + ":\n"
            agfi_entry += "    agfi: " + agfi + "\n"
            agfi_entry += "    deploy_triplet_override: null\n"
            agfi_entry += "    custom_runtime_config: null\n"
            message_body = "Your AGFI has been created!\nAdd\n\n" + agfi_entry + "\nto your config_hwdb.yaml to use this hardware configuration."

            send_firesim_notification(message_title, message_body)

            rootLogger.info(message_title)
            rootLogger.info(message_body)

            # for convenience when generating a bunch of images. you can just
            # cat all the files in this directory after your builds finish to get
            # all the entries to copy into config_hwdb.yaml
            hwdb_entry_file_location = f"{local_deploy_dir}/built-hwdb-entries/"
            local("mkdir -p " + hwdb_entry_file_location)
            with open(hwdb_entry_file_location + "/" + afiname, "w") as outputfile:
            outputfile.write(agfi_entry)

            if build_config.post_build_hook:
            with StreamLogger('stdout'), StreamLogger('stderr'):
                localcap = local(f"{build_config.post_build_hook} {local_results_dir}", capture=True)
                rootLogger.debug("[localhost] " + str(localcap))
                rootLogger.debug("[localhost] " + str(localcap.stderr))

            rootLogger.info(f"Build complete! AFI ready. See {os.path.join(hwdb_entry_file_location,afiname)}.")
            return True
        else:
            return None
