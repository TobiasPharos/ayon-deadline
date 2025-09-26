import os
import re
from dataclasses import dataclass, field, asdict
import copy

import pyblish.api

from ayon_core.lib import BoolDef
from ayon_core.pipeline.publish import (
    AYONPyblishPluginMixin
)
from ayon_core.lib import (
    is_in_tests,
    BoolDef,
    NumberDef
)
from ayon_core.settings import get_project_settings
from ayon_deadline.abstract_submit_deadline import requests_post
from ayon_deadline.lib import get_instance_job_envs, get_ayon_render_job_envs


DEADLINE_SETTINGS = get_project_settings(os.getenv("AYON_PROJECT_NAME"))[
    "deadline"
]


class NukeSubmitDeadline(pyblish.api.InstancePlugin, AYONPyblishPluginMixin):
    """Submit write to Deadline

    Renders are submitted to a Deadline Web Service as
    supplied via settings key "DEADLINE_REST_URL".

    """

    label = "Submit Nuke to Deadline"
    order = pyblish.api.IntegratorOrder + 0.1
    hosts = ["nuke"]
    families = ["render", "prerender"]
    optional = True
    targets = ["local"]
    settings_category = "deadline"

    use_gpu = None
    node_class_limit_groups = {}

    def process(self, instance):
        """Plugin entry point."""
        if not instance.data.get("farm"):
            self.log.debug("Should not be processed on farm, skipping.")
            return

        self._instance = instance

        context = instance.context

        deadline_url = instance.data["deadline"]["url"]
        assert deadline_url, "Requires Deadline Webservice URL"

        self.deadline_url = "{}/api/jobs".format(deadline_url)
        self._comment = context.data.get("comment", "")
        self._ver = re.search(r"\d+\.\d+", context.data.get("hostVersion"))
        self._deadline_user = context.data.get(
            "deadlineUser", getpass.getuser()
        )
        submit_frame_start = int(instance.data["frameStartHandle"])
        submit_frame_end = int(instance.data["frameEndHandle"])

        # get output path
        render_path = instance.data["path"]
        script_path = context.data["currentFile"]

        use_published_workfile = instance.data["attributeValues"].get(
            "use_published_workfile", self.use_published_workfile
        )
        if use_published_workfile:
            script_path = self._get_published_workfile_path(context)

        # only add main rendering job if target is not frames_farm
        r_job_response_json = None
        if instance.data["render_target"] != "frames_farm":
            r_job_response = self.payload_submit(
                instance,
                script_path,
                render_path,
                node.name(),
                submit_frame_start,
                submit_frame_end,
            )
            r_job_response_json = r_job_response.json()
            instance.data["deadlineSubmissionJob"] = r_job_response_json

            # Store output dir for unified publisher (filesequence)
            instance.data["outputDir"] = os.path.dirname(render_path).replace(
                "\\", "/"
            )
            instance.data["publishJobState"] = "Suspended"

        if instance.data.get("bakingNukeScripts"):
            for baking_script in instance.data["bakingNukeScripts"]:
                render_path = baking_script["bakeRenderPath"]
                script_path = baking_script["bakeScriptPath"]
                exe_node_name = baking_script["bakeWriteNodeName"]

                b_job_response = self.payload_submit(
                    instance,
                    script_path,
                    render_path,
                    exe_node_name,
                    submit_frame_start,
                    submit_frame_end,
                    r_job_response_json,
                    baking_submission=True,
                )

                # Store output dir for unified publisher (filesequence)
                instance.data["deadlineSubmissionJob"] = b_job_response.json()

                instance.data["publishJobState"] = "Suspended"

                # add to list of job Id
                if not instance.data.get("bakingSubmissionJobs"):
                    instance.data["bakingSubmissionJobs"] = []

                instance.data["bakingSubmissionJobs"].append(
                    b_job_response.json()["_id"]
                )

        # redefinition of families
        if "render" in instance.data["productType"]:
            instance.data["family"] = "write"
            instance.data["productType"] = "write"
            families.insert(0, "render2d")
        elif "prerender" in instance.data["productType"]:
            instance.data["family"] = "write"
            instance.data["productType"] = "write"
            families.insert(0, "prerender")
        instance.data["families"] = families

    def _get_published_workfile_path(self, context):
        """This method is temporary while the class is not inherited from
        AbstractSubmitDeadline"""
        anatomy = context.data["anatomy"]
        self.log.debug(f"{context.data=}")
        self.log.debug(f"{DEADLINE_SETTINGS=}")
        nuke_settings = DEADLINE_SETTINGS["publish"]["NukeSubmitDeadline"]
        publish_default_template = nuke_settings.get(
            "publish_default_template"
        )
        publish_template = anatomy.get_template_item(
            "publish", publish_default_template, "path"
        )
        for instance in context:
            if (
                instance.data["productType"] != "workfile"
                # Disabled instances won't be integrated
                or instance.data("publish") is False
            ):
                continue
            template_data = instance.data["anatomyData"]
            # Expect workfile instance has only one representation
            representation = instance.data["representations"][0]
            # Get workfile extension
            repre_file = representation["files"]
            self.log.info(repre_file)
            ext = os.path.splitext(repre_file)[1].lstrip(".")

            # Fill template data
            template_data["representation"] = representation["name"]
            template_data["ext"] = ext
            template_data["comment"] = None

            template_filled = publish_template.format(template_data)
            script_path = os.path.normpath(template_filled)
            self.log.info(
                "Using published scene for render {}".format(script_path)
            )
            return script_path

        return None

    def payload_submit(
        self,
        instance,
        script_path,
        render_path,
        exe_node_name,
        start_frame,
        end_frame,
        response_data=None,
        baking_submission=False,
    ):
        """Submit payload to Deadline

        Args:
            instance (pyblish.api.Instance): pyblish instance
            script_path (str): path to nuke script
            render_path (str): path to rendered images
            exe_node_name (str): name of the node to render
            start_frame (int): start frame
            end_frame (int): end frame
            response_data Optional[dict]: response data from
                                          previous submission
            baking_submission Optional[bool]: if it's baking submission

        Returns:
            requests.Response
        """
        render_dir = os.path.normpath(os.path.dirname(render_path))

        # batch name
        src_filepath = instance.context.data["currentFile"]
        batch_name = os.path.basename(src_filepath)
        job_name = os.path.basename(render_path)

        if is_in_tests():
            batch_name += datetime.now().strftime("%d%m%Y%H%M%S")

        output_filename_0 = self.preview_fname(render_path)

        if not response_data:
            response_data = {}

        try:
            # Ensure render folder exists
            os.makedirs(render_dir)
        except OSError:
            pass

        # resolve any limit groups
        limit_groups = self.get_limit_groups()
        self.log.debug("Limit groups: `{}`".format(limit_groups))

        # Plugin Name
        nuke_settings = DEADLINE_SETTINGS["publish"]["NukeSubmitDeadline"]
        plugin_name = nuke_settings.get("plugin_name", "Nuke")

        payload = {
            "JobInfo": {
                # Top-level group name
                "BatchName": batch_name,
                # Job name, as seen in Monitor
                "Name": job_name,
                # Arbitrary username, for visualisation in Monitor
                "UserName": self._deadline_user,
                "Priority": instance.data["attributeValues"].get(
                    "priority", self.priority
                ),
                "ChunkSize": instance.data["attributeValues"].get(
                    "chunk", self.chunk_size
                ),
                "ConcurrentTasks": instance.data["attributeValues"].get(
                    "concurrency", self.concurrent_tasks
                ),
                "Department": self.department,
                "Pool": instance.data.get("primaryPool"),
                "SecondaryPool": instance.data.get("secondaryPool"),
                "Group": self.group,
                "Plugin": plugin_name,
                "Frames": "{start}-{end}".format(
                    start=start_frame, end=end_frame
                ),
                "Comment": self._comment,
                # Optional, enable double-click to preview rendered
                # frames from Deadline Monitor
                "OutputFilename0": output_filename_0.replace("\\", "/"),
                # limiting groups
                "LimitGroups": ",".join(limit_groups),
            },
            "PluginInfo": {
                # Input
                "SceneFile": script_path,
                # Output directory and filename
                "OutputFilePath": render_dir.replace("\\", "/"),
                # "OutputFilePrefix": render_variables["filename_prefix"],
                # Mandatory for Deadline
                "Version": self._ver.group(),
                # Resolve relative references
                "ProjectPath": script_path,
                "AWSAssetFile0": render_path,
                # using GPU by default
                "UseGpu": instance.data["attributeValues"].get(
                    "use_gpu", self.use_gpu
                ),
                # Only the specific write node is rendered.
                "WriteNode": exe_node_name,
            },
            # Mandatory for Deadline, may be empty
            "AuxFiles": [],
        }

        # Add workfile dependency.
        workfile_dependency = instance.data["attributeValues"].get(
            "workfile_dependency", self.workfile_dependency
        )
        if workfile_dependency:
            payload["JobInfo"].update({"AssetDependency0": script_path})

        # TODO: rewrite for baking with sequences
        if baking_submission:
            payload["JobInfo"].update(
                {"JobType": "Normal", "ChunkSize": 99999999}
            )

        if response_data.get("_id"):
            payload["JobInfo"].update(
                {
                    "BatchName": response_data["Props"]["Batch"],
                    "JobDependency0": response_data["_id"],
                }
            )

        # Include critical environment variables with submission
        keys = [
            "NUKE_PATH",
            "FOUNDRY_LICENSE",
            "PHAROS_LOCATION",
            "PHAROS_NUKELIB",
            "CCCID",
            "NUKE_FONT_PATH",
            "OCIO",
            "PROJECT_LUT",
            "SHOT_LUT",
        ]

        # add allowed keys from preset if any
        if self.env_allowed_keys:
            keys += self.env_allowed_keys

        nuke_specific_env = {
            key: os.environ[key] for key in keys if key in os.environ
        }

        # Set job environment variables
        environment = get_instance_job_envs(instance)
        environment.update(get_ayon_render_job_envs())
        environment.update(nuke_specific_env)

        # finally search replace in values of any key
        if self.env_search_replace_values:
            for key, value in environment.items():
                for item in self.env_search_replace_values:
                    environment[key] = value.replace(
                        item["name"], item["value"]
                    )

        payload["JobInfo"].update(
            {
                "EnvironmentKeyValue%d" % index: "{key}={value}".format(
                    key=key, value=environment[key]
                )
                for index, key in enumerate(environment)
            }
        )

        plugin = payload["JobInfo"]["Plugin"]
        self.log.debug("using render plugin : {}".format(plugin))

        self.log.debug("Submitting..")
        self.log.debug(json.dumps(payload, indent=4, sort_keys=True))

        # adding expected files to instance.data
        write_node = instance.data["transientData"]["node"]
        render_path = instance.data["path"]
        start_frame = int(instance.data["frameStartHandle"])
        end_frame = int(instance.data["frameEndHandle"])
        self._expected_files(
            instance,
            render_path,
            start_frame,
            end_frame
        )

        job_info = self.get_generic_job_info(instance)
        self.job_info = self.get_job_info(job_info=job_info)

        self._set_scene_path(
            context.data["currentFile"],
            job_info.use_published,
            instance.data.get("stagingDir_is_custom", False)
        )

        self._append_job_output_paths(
            instance,
            self.job_info
        )

        self.plugin_info = self.get_plugin_info(
            scene_path=self.scene_path,
            render_path=render_path,
            write_node_name=write_node.name()
        )

        self.aux_files = self.get_aux_files()

        plugin_info_data = instance.data["deadline"]["plugin_info_data"]
        if plugin_info_data:
            self.apply_additional_plugin_info(plugin_info_data)

        if instance.data["render_target"] != "frames_farm":
            job_id = self.process_submission()
            self.log.info("Submitted job to Deadline: {}.".format(job_id))

            render_path = instance.data["path"]
            instance.data["outputDir"] = os.path.dirname(
                render_path).replace("\\", "/")

        if instance.data.get("bakingNukeScripts"):
            for baking_script in instance.data["bakingNukeScripts"]:
                self.job_info = copy.deepcopy(self.job_info)
                self.job_info.JobType = "Normal"

                response_data = instance.data.get("deadlineSubmissionJob", {})
                # frames_farm instance doesn't have render submission
                if response_data.get("_id"):
                    self.job_info.BatchName = response_data["Props"]["Batch"]
                    self.job_info.JobDependencies.append(response_data["_id"])

                render_path = baking_script["bakeRenderPath"]
                scene_path = baking_script["bakeScriptPath"]
                write_node_name = baking_script["bakeWriteNodeName"]

                self.job_info.Name = os.path.basename(render_path)

                # baking job shouldn't be split
                self.job_info.ChunkSize = 999999

                self.job_info.Frames = f"{start_frame}-{end_frame}"

                self.plugin_info = self.get_plugin_info(
                    scene_path=scene_path,
                    render_path=render_path,
                    write_node_name=write_node_name
                )
                job_id = self.process_submission()
                self.log.info(
                    "Submitted baking job to Deadline: {}.".format(job_id))

                # add to list of job Id
                if not instance.data.get("bakingSubmissionJobs"):
                    instance.data["bakingSubmissionJobs"] = []

                instance.data["bakingSubmissionJobs"].append(job_id)

    def get_job_info(self, job_info=None, **kwargs):
        instance = self._instance

        job_info.Plugin = "Nuke"

        start_frame = int(instance.data["frameStartHandle"])
        end_frame = int(instance.data["frameEndHandle"])
        # already collected explicit values for rendered Frames
        if not job_info.Frames:
            job_info.Frames = "{start}-{end}".format(
                start=start_frame,
                end=end_frame
            )
        limit_groups = self._get_limit_groups(self.node_class_limit_groups)
        job_info.LimitGroups.extend(limit_groups)

        render_path = instance.data["path"]
        job_info.Name = os.path.basename(render_path)

        return job_info

    def get_plugin_info(
            self, scene_path=None, render_path=None, write_node_name=None):
        instance = self._instance
        context = instance.context
        version = re.search(r"\d+\.\d+", context.data.get("hostVersion"))

        attribute_values = self.get_attr_values_from_data(instance.data)

        render_dir = os.path.dirname(render_path)
        plugin_info = NukePluginInfo(
            SceneFile=scene_path,
            Version=version.group(),
            OutputFilePath=render_dir.replace("\\", "/"),
            ProjectPath=scene_path,
            UseGpu=attribute_values["use_gpu"],
            WriteNode=write_node_name
        )

        plugin_payload: dict = asdict(plugin_info)
        return plugin_payload

    @classmethod
    def get_attribute_defs(cls):
        return [
            BoolDef(
                "use_gpu",
                label="Use GPU",
                default=cls.use_gpu,
            ),
        ]

    def _get_limit_groups(self, limit_groups):
        """Search for limit group nodes and return group name.
        Limit groups will be defined as pairs in Nuke deadline submitter
        presents where the key will be name of limit group and value will be
        a list of plugin's node class names. Thus, when a plugin uses more
        than one node, these will be captured and the triggered process
        will add the appropriate limit group to the payload jobinfo attributes.
        Returning:
            list: captured groups list
        """
        # Not all hosts can import this module.
        import nuke

        captured_groups = []
        for limit_group in limit_groups:
            lg_name = limit_group["name"]

            for node_class in limit_group["value"]:
                for node in nuke.allNodes(recurseGroups=True):
                    # ignore all nodes not member of defined class
                    if node.Class() not in node_class:
                        continue
                    # ignore all disabled nodes
                    if node["disable"].value():
                        continue
                    # add group name if not already added
                    if lg_name not in captured_groups:
                        captured_groups.append(lg_name)
        return captured_groups

    def _expected_files(
        self,
        instance,
        filepath,
        start_frame,
        end_frame
    ):
        """ Create expected files in instance data
        """
        if instance.data["render_target"] == "frames_farm":
            self.log.debug(
                "Expected files already collected for 'frames_farm', skipping."
            )
            return

        if not instance.data.get("expectedFiles"):
            instance.data["expectedFiles"] = []

        dirname = os.path.dirname(filepath)
        file = os.path.basename(filepath)

        # since some files might be already tagged as publish_on_farm
        # we need to avoid adding them to expected files since those would be
        # duplicated into metadata.json file
        representations = instance.data.get("representations", [])
        # check if file is not in representations with publish_on_farm tag
        for repre in representations:
            # Skip if 'publish_on_farm' not available
            if "publish_on_farm" not in repre.get("tags", []):
                continue

            # in case where single file (video, image) is already in
            # representation file. Will be added to expected files via
            # submit_publish_job.py
            if file in repre.get("files", []):
                self.log.debug("Skipping expected file: {}".format(filepath))
                return

        # in case path is hashed sequence expression
        # (e.g. /path/to/file.####.png)
        if "#" in file:
            pparts = file.split("#")
            padding = "%0{}d".format(len(pparts) - 1)
            file = pparts[0] + padding + pparts[-1]

        # in case input path was single file (video or image)
        if "%" not in file:
            instance.data["expectedFiles"].append(filepath)
            return

        # shift start frame by 1 if slate is present
        if instance.data.get("slate"):
            start_frame -= 1

        # add sequence files to expected files
        for i in range(start_frame, (end_frame + 1)):
            instance.data["expectedFiles"].append(
                os.path.join(dirname, (file % i)).replace("\\", "/"))
