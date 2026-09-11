import hashlib
import json
import logging
import os

from dataclasses import dataclass
from typing import Any, Optional, Union, cast

from swebench.harness.constants import (
    DEFAULT_DOCKER_SPECS,
    KEY_INSTANCE_ID,
    LATEST,
    MAP_REPO_TO_EXT,
    MAP_REPO_VERSION_TO_SPECS,
    SWEbenchInstance,
)
from swebench.harness.dockerfiles import (
    get_dockerfile_base,
    get_dockerfile_env,
    get_dockerfile_instance,
)
from swebench.harness.test_spec.create_scripts import (
    make_repo_script_list,
    make_env_script_list,
    make_eval_script_list,
)

# Environment variable for custom docker registry URL (replaces default "docker.io")
DOCKER_REGISTRY_ENV = "DOCKER_REGISTRY"

# Dataset columns to check (in priority order) for custom docker image names
# Dataset columns checked (in priority order) for custom docker image refs/URLs.
# "image_url" is first but validated as a Docker ref (must not contain "://").
DOCKER_IMAGE_COLUMNS = ("image_url", "docker_image", "base_image", "image_name")

EMPTY_PATCH = """\
diff --git a/empty.txt b/empty.txt
index e69de29..e69de29 100644
--- a/empty.txt
+++ b/empty.txt"""


def _apply_registry(image_name: str) -> str:
    """Prepend a custom registry URL from DOCKER_REGISTRY env var if set.

    If the image already contains a '/' indicating a registry/namespace prefix,
    the registry is prepended before the first segment. If DOCKER_REGISTRY is
    not set, the image name is returned unchanged.
    """
    registry = os.environ.get(DOCKER_REGISTRY_ENV)
    if not registry:
        return image_name
    # Strip trailing slash from registry
    registry = registry.rstrip("/")
    # If image already starts with the registry, return as-is
    if image_name.startswith(f"{registry}/"):
        return image_name
    return f"{registry}/{image_name}"


@dataclass
class TestSpec:
    """
    A dataclass that represents a test specification for a single instance of SWE-bench.
    """

    instance_id: str
    repo: str
    version: str
    repo_script_list: list[str]
    eval_script_list: list[str]
    env_script_list: list[str]
    arch: str
    FAIL_TO_PASS: list[str]
    PASS_TO_PASS: list[str]
    language: str
    docker_specs: dict
    namespace: Optional[str]
    base_image_tag: str = LATEST
    env_image_tag: str = LATEST
    instance_image_tag: str = LATEST
    custom_image_name: Optional[str] = None
    # True when custom_image_name came from a dataset column (image_url, docker_image,
    # etc.), meaning the image should be pulled fresh and removed after evaluation.
    # False for images derived from --image_naming_pattern swesmith, which are reused
    # across instances and should not be auto-removed.
    is_dataset_image: bool = False

    @property
    def setup_env_script(self):
        return (
            "\n".join(["#!/bin/bash", "set -euxo pipefail"] + self.env_script_list)
            + "\n"
        )

    @property
    def eval_script(self):
        return (
            "\n".join(["#!/bin/bash", "set -uxo pipefail"] + self.eval_script_list)
            + "\n"
        )
        # Don't exit early because we need to revert tests at the end

    @property
    def install_repo_script(self):
        return (
            "\n".join(["#!/bin/bash", "set -euxo pipefail"] + self.repo_script_list)
            + "\n"
        )

    @property
    def base_image_key(self):
        """
        If docker_specs are present, the base image key includes a hash of the specs.
        """
        if self.docker_specs != {}:
            hash_key = str(self.docker_specs)
            hash_object = hashlib.sha256()
            hash_object.update(hash_key.encode("utf-8"))
            hash_value = hash_object.hexdigest()
            val = hash_value[
                :10
            ]  # 10 characters is still likely to be unique given only a few base images will be created
            return f"sweb.base.{MAP_REPO_TO_EXT[self.repo]}.{self.arch}.{val}:{self.base_image_tag}"
        return (
            f"sweb.base.{MAP_REPO_TO_EXT[self.repo]}.{self.arch}:{self.base_image_tag}"
        )

    @property
    def env_image_key(self):
        """
        The key for the environment image is based on the hash of the environment script list.
        If the environment script list changes, the image will be rebuilt automatically.

        Note that old images are not automatically deleted, so consider cleaning up old images periodically.
        """
        hash_key = str(self.env_script_list)
        if self.docker_specs != {}:
            hash_key += str(self.docker_specs)
        hash_object = hashlib.sha256()
        hash_object.update(hash_key.encode("utf-8"))
        hash_value = hash_object.hexdigest()
        val = hash_value[:22]  # 22 characters is still very likely to be unique
        return f"sweb.env.{MAP_REPO_TO_EXT[self.repo]}.{self.arch}.{val}:{self.env_image_tag}"

    @property
    def instance_image_key(self):
        if self.custom_image_name is not None:
            return _apply_registry(self.custom_image_name)
        key = f"sweb.eval.{self.arch}.{self.instance_id.lower()}:{self.instance_image_tag}"
        if self.namespace is not None:
            key = f"{self.namespace}/{key}".replace("__", "_1776_")
            key = _apply_registry(key)
        return key

    @property
    def is_remote_image(self):
        return self.namespace is not None or self.custom_image_name is not None

    def get_instance_container_name(self, run_id=None):
        if not run_id:
            return f"sweb.eval.{self.instance_id}"
        return f"sweb.eval.{self.instance_id.lower()}.{run_id}"

    @property
    def base_dockerfile(self):
        return get_dockerfile_base(
            self.platform,
            self.arch,
            self.language,
            **{**DEFAULT_DOCKER_SPECS, **self.docker_specs},
        )

    @property
    def env_dockerfile(self):
        return get_dockerfile_env(
            self.platform,
            self.arch,
            self.language,
            self.base_image_key,
            **{**DEFAULT_DOCKER_SPECS, **self.docker_specs},
        )

    @property
    def instance_dockerfile(self):
        return get_dockerfile_instance(self.platform, self.language, self.env_image_key)

    @property
    def platform(self):
        if self.arch == "x86_64":
            return "linux/x86_64"
        elif self.arch == "arm64":
            return "linux/arm64/v8"
        else:
            raise ValueError(f"Invalid architecture: {self.arch}")


def get_test_specs_from_dataset(
    dataset: Union[list[SWEbenchInstance], list[TestSpec]],
    namespace: Optional[str] = None,
    instance_image_tag: str = LATEST,
    env_image_tag: str = LATEST,
    image_naming_pattern: str = "swebench",
) -> list[TestSpec]:
    """
    Idempotent function that converts a list of SWEbenchInstance objects to a list of TestSpec objects.
    """
    if isinstance(dataset[0], TestSpec):
        return cast(list[TestSpec], dataset)
    return list(
        map(
            lambda x: make_test_spec(
                x,
                namespace,
                instance_image_tag,
                env_image_tag,
                image_naming_pattern=image_naming_pattern,
            ),
            cast(list[SWEbenchInstance], dataset),
        )
    )


def make_test_spec(
    instance: SWEbenchInstance,
    namespace: Optional[str] = None,
    base_image_tag: str = LATEST,
    env_image_tag: str = LATEST,
    instance_image_tag: str = LATEST,
    arch: str = "x86_64",
    image_naming_pattern: str = "swebench",
) -> TestSpec:
    if isinstance(instance, TestSpec):
        return instance
    assert base_image_tag is not None, "base_image_tag cannot be None"
    assert env_image_tag is not None, "env_image_tag cannot be None"
    assert instance_image_tag is not None, "instance_image_tag cannot be None"
    instance_id = instance[KEY_INSTANCE_ID]
    repo = instance["repo"]
    version = instance.get("version")
    base_commit = instance["base_commit"]
    problem_statement = instance.get("problem_statement")
    hints_text = instance.get("hints_text")  # Unused
    test_patch = instance.get("test_patch", EMPTY_PATCH)

    # Detect custom docker image ref from dataset columns.
    # image_url is validated as a Docker ref (must not look like an HTTP URL).
    custom_image_name = None
    is_dataset_image = False
    invalid_col: tuple[str, object] | None = None  # first invalid-but-present column
    for col in DOCKER_IMAGE_COLUMNS:
        val = instance.get(col)  # type: ignore[arg-type]
        if not val:
            continue
        if not isinstance(val, str) or "://" in val:
            # Record the first bad column for a clear error if no valid one is found.
            if invalid_col is None:
                invalid_col = (col, val)
            continue
        custom_image_name = val
        is_dataset_image = True
        break

    # If every non-empty image column was invalid, fail clearly rather than
    # silently evaluating against an unrelated locally-built image.
    if not is_dataset_image and invalid_col is not None:
        bad_col, bad_val = invalid_col
        raise ValueError(
            f"Instance '{instance_id}': column '{bad_col}' value {bad_val!r} is not "
            "a valid Docker image reference (expected a plain string without a URL "
            "scheme such as 'https://') and no other image column provided a valid "
            "reference. Correct the dataset entry."
        )

    # If no explicit image column but swesmith pattern requested, generate it.
    # These images are not pulled fresh per-instance; they are reused across runs.
    if custom_image_name is None and image_naming_pattern == "swesmith":
        owner, repo_name = repo.split("/")
        custom_image_name = (
            f"{namespace or 'swebench'}/swesmith.{arch}"
            f".{owner}_1776_{repo_name}.{base_commit[:8]}"
        ).lower()

    def _from_json_or_obj(key: str) -> Any:
        """If key points to string, load with json"""
        if key not in instance:
            # If P2P, F2P keys not found, it's a validation instance
            return []
        if isinstance(instance[key], str):
            return json.loads(instance[key])
        return instance[key]

    pass_to_pass = _from_json_or_obj("PASS_TO_PASS")
    fail_to_pass = _from_json_or_obj("FAIL_TO_PASS")

    env_name = "testbed"
    repo_directory = f"/{env_name}"
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
    docker_specs = specs.get("docker_specs", {})

    repo_script_list = make_repo_script_list(
        specs, repo, repo_directory, base_commit, env_name
    )
    env_script_list = make_env_script_list(instance, specs, env_name)
    eval_script_list = make_eval_script_list(
        instance, specs, env_name, repo_directory, base_commit, test_patch
    )
    return TestSpec(
        instance_id=instance_id,
        repo=repo,
        env_script_list=env_script_list,
        repo_script_list=repo_script_list,
        eval_script_list=eval_script_list,
        version=version,
        arch=arch,
        FAIL_TO_PASS=fail_to_pass,
        PASS_TO_PASS=pass_to_pass,
        language=MAP_REPO_TO_EXT.get(repo, "python"),
        docker_specs=docker_specs,
        namespace=namespace,
        base_image_tag=base_image_tag,
        env_image_tag=env_image_tag,
        instance_image_tag=instance_image_tag,
        custom_image_name=custom_image_name,
        is_dataset_image=is_dataset_image,
    )
