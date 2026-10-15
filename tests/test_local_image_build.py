# Licensed under the Apache License, Version 2.0. See LICENSE.

"""Opt-in checks of a locally built image; never contact a Kubernetes cluster."""

import json
import os
import re
import subprocess

import pytest

IMAGE = os.environ.get("CRATE_OPERATOR_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Set CRATE_OPERATOR_TEST_IMAGE to verify a local image"
)


def run_image(*args):
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--entrypoint",
            "python",
            IMAGE,
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout


def test_image_installs_patched_distribution_and_internal_service():
    result = json.loads(
        run_image(
            "-c",
            "import importlib.metadata,json;"
            "from crate.operator.create import get_data_service;"
            "s=get_data_service(None,'test',{},4200,5432,None,exposure='clusterip');"
            "print(json.dumps({'version':importlib.metadata.version('crate-operator'),"
            "'type':s.spec.type,'annotations':s.metadata.annotations,"
            "'ports':[p.port for p in s.spec.ports]}))",
        )
    )
    assert result == {
        "version": "2.63.0+hive.1",
        "type": "ClusterIP",
        "annotations": {},
        "ports": [4200, 5432],
    }


def test_image_retains_license_and_nonroot_runtime():
    result = json.loads(
        run_image(
            "-c",
            "import json,os,pathlib;"
            "p=pathlib.Path('/usr/share/licenses/crate-operator/LICENSE');"
            "print(json.dumps({'uid':os.getuid(),"
            "'license':p.is_file() and 'Apache License' in p.read_text()}))",
        )
    )
    assert result["uid"] != 0
    assert result["license"] is True


def test_image_provenance_and_upstream_entrypoint():
    result = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    image = json.loads(result.stdout)[0]
    assert image["Architecture"] == "amd64"
    config = image["Config"]
    assert config["User"] == "crate-operator"
    assert config["Entrypoint"] == ["kopf", "run", "--standalone", "-A"]
    assert config["Cmd"] == ["main.py"]
    labels = config["Labels"]
    assert labels["org.opencontainers.image.version"] == "2.63.0+hive.1"
    assert labels["org.opencontainers.image.source"] == (
        "https://github.com/itenev/crate-operator"
    )
    revision = labels["org.opencontainers.image.revision"]
    assert re.fullmatch(r"[0-9a-f]{40}", revision)
    expected_revision = os.environ.get("CRATE_OPERATOR_TEST_REVISION")
    if expected_revision:
        assert revision == expected_revision
    source_state = os.environ.get("CRATE_OPERATOR_TEST_SOURCE_STATE", "dirty")
    assert source_state in {"clean", "dirty"}
    assert labels["io.hive.source.state"] == source_state
    patch_sha256 = labels["io.hive.source.patch-sha256"]
    if source_state == "dirty":
        assert re.fullmatch(r"[0-9a-f]{64}", patch_sha256)
    else:
        assert patch_sha256 == ""
    assert labels["org.opencontainers.image.base.name"].startswith(
        "python:3.12-slim@sha256:"
    )


def test_image_can_load_operator_and_execute_entrypoint_help():
    run_image("-c", "import crate.operator.main")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            IMAGE,
            "--help",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert "Usage:" in result.stdout
