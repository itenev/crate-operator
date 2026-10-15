# Licensed under the Apache License, Version 2.0. See LICENSE.

"""Destructive opt-in checks of the named, disposable Minikube smoke cluster."""

import json
import os
import subprocess
import time
from copy import deepcopy

import pytest

NAMESPACE = "crate-validation"
NAME = "clusterip-smoke"
POD = f"crate-data-hot-{NAME}-0"
SERVICE = f"crate-{NAME}"
pytestmark = pytest.mark.skipif(
    os.environ.get("CRATE_OPERATOR_TEST_CLUSTERIP") != "true",
    reason="Requires the prepared disposable crate-clusterip Minikube profile",
)


@pytest.fixture
def kubectl(request):
    kubeconfig = request.config.getoption("--kube-config")
    context = request.config.getoption("--kube-context")
    assert kubeconfig, "Supply the dedicated --kube-config"
    assert context == "crate-clusterip", "Never run against another context"

    def run(*args, body=None):
        result = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig, "--context", context, *args],
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=240,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    node = json.loads(run("get", "node", "crate-clusterip", "-o", "json"))
    assert node["metadata"]["labels"]["minikube.k8s.io/name"] == context
    return run


def resource(kubectl, kind, name):
    return json.loads(kubectl("get", kind, name, "-n", NAMESPACE, "-o", "json"))


def wait_for(check, description, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    pytest.fail(f"Timed out waiting for {description}")


def assert_internal_service(kubectl):
    service = resource(kubectl, "service", SERVICE)
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["clusterIP"] != "None"
    assert [port["port"] for port in service["spec"]["ports"]] == [4200, 5432]
    assert not service.get("status", {}).get("loadBalancer", {}).get("ingress")
    assert not any(
        key.startswith("external-dns.") or "load-balancer" in key
        for key in service["metadata"].get("annotations", {})
    )
    slices = json.loads(
        kubectl(
            "get",
            "endpointslices",
            "-n",
            NAMESPACE,
            "-l",
            f"kubernetes.io/service-name={SERVICE}",
            "-o",
            "json",
        )
    )
    assert any(
        endpoint["conditions"].get("ready")
        for item in slices["items"]
        for endpoint in item.get("endpoints", [])
    )
    assert not json.loads(kubectl("get", "ingress", "-n", NAMESPACE, "-o", "json"))[
        "items"
    ]
    return service["metadata"]["uid"]


def assert_sql_from_operator(kubectl):
    # Credentials stay inside the operator Pod; only the row count is returned.
    script = f"""
import asyncio, json
from kubernetes_asyncio import config
from kubernetes_asyncio.client import CoreV1Api
from crate.operator.cratedb import connection_factory
from crate.operator.utils.k8s_api_client import GlobalApiClient
from crate.operator.utils.kubeapi import get_system_user_password
async def main():
    config.load_incluster_config()
    async with GlobalApiClient() as api:
        password = await get_system_user_password(
            CoreV1Api(api), {NAMESPACE!r}, {NAME!r}
        )
    factory = connection_factory({SERVICE + '.' + NAMESPACE!r}, password)
    async with factory() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT count(*) FROM sys.nodes")
            print(json.dumps(await cursor.fetchone()))
asyncio.run(main())
"""
    result = kubectl(
        "exec",
        "-n",
        "crate-operator",
        "deployment/crate-operator",
        "--",
        "python",
        "-c",
        script,
    )
    assert json.loads(result) == [1]


def update_finished(kubectl, replicas):
    body = resource(kubectl, "cratedb", NAME)
    annotations = body["metadata"].get("annotations", {})
    last = json.loads(annotations.get("operator.cloud.crate.io/last", "{}"))
    return (
        last.get("spec") == body["spec"]
        and last.get("spec", {}).get("nodes", {}).get("data", [{}])[0].get("replicas")
        == replicas
        and not any(
            key.startswith("operator.cloud.crate.io/cluster_update")
            for key in annotations
        )
    )


def rejection_events(kubectl, uid, handler, reason):
    events = json.loads(
        kubectl(
            "get",
            "events",
            "-n",
            NAMESPACE,
            "--field-selector",
            f"involvedObject.uid={uid}",
            "-o",
            "json",
        )
    )
    return {
        event["metadata"]["uid"]: event["metadata"]["resourceVersion"]
        for event in events["items"]
        if f"Handler '{handler}' failed permanently: " in event.get("message", "")
        and reason in event.get("message", "")
    }


def test_internal_bootstrap_routing_and_sql(kubectl):
    body = resource(kubectl, "cratedb", NAME)
    assert body["spec"]["cluster"]["exposure"] == "clusterip"
    assert body["status"]["cluster_create/bootstrap"]["success"] is True
    wait_for(
        lambda: resource(kubectl, "cratedb", NAME)["status"]
        .get("crateDBStatus", {})
        .get("health")
        == "GREEN",
        "settled database health",
        timeout=90,
    )
    assert_internal_service(kubectl)
    assert_sql_from_operator(kubectl)
    api_resources = kubectl("api-resources", "--api-group=traefik.io", "-o", "name")
    assert not api_resources.strip(), "This test must not depend on Traefik CRDs"


def test_internal_suspend_resume_recreates_service(kubectl):
    original_uid = assert_internal_service(kubectl)
    assert_sql_from_operator(kubectl)
    nodes = resource(kubectl, "cratedb", NAME)["spec"]["nodes"]
    suspended = json.loads(json.dumps(nodes))
    suspended["data"][0]["replicas"] = 0
    try:
        kubectl(
            "patch",
            "cratedb",
            NAME,
            "-n",
            NAMESPACE,
            "--type=merge",
            "-p",
            json.dumps({"spec": {"nodes": suspended}}),
        )
        wait_for(
            lambda: not json.loads(
                kubectl("get", "pods", "-n", NAMESPACE, "-o", "json")
            )["items"],
            "database Pod removal during suspend",
        )
        wait_for(
            lambda: not json.loads(
                kubectl(
                    "get",
                    "service",
                    SERVICE,
                    "-n",
                    NAMESPACE,
                    "--ignore-not-found",
                    "-o",
                    "json",
                )
                or "{}"
            ),
            "client Service removal during suspend",
        )
        wait_for(lambda: update_finished(kubectl, 0), "completed suspend handler")
    finally:
        kubectl(
            "patch",
            "cratedb",
            NAME,
            "-n",
            NAMESPACE,
            "--type=merge",
            "-p",
            json.dumps({"spec": {"nodes": nodes}}),
        )
    wait_for(
        lambda: json.loads(
            kubectl(
                "get",
                "pod",
                POD,
                "-n",
                NAMESPACE,
                "--ignore-not-found",
                "-o",
                "json",
            )
            or "{}"
        )
        .get("status", {})
        .get("phase")
        == "Running",
        "resumed database Pod",
    )
    kubectl(
        "wait", "--for=condition=Ready", f"pod/{POD}", "-n", NAMESPACE, "--timeout=180s"
    )
    assert assert_internal_service(kubectl) != original_uid
    assert_sql_from_operator(kubectl)
    wait_for(lambda: update_finished(kubectl, 1), "completed resume handler")


@pytest.mark.parametrize(
    "cluster_extra,extra",
    [
        ({"externalDNS": "invalid.example.test"}, {}),
        (
            {
                "service": {
                    "annotations": {
                        "service.beta.kubernetes.io/aws-load-balancer-type": "nlb"
                    }
                }
            },
            {},
        ),
        (
            {
                "service": {
                    "annotations": {
                        "external-dns.alpha.kubernetes.io/hostname": (
                            "invalid.example.test"
                        )
                    }
                }
            },
            {},
        ),
        (
            {},
            {
                "grandCentral": {
                    "backendEnabled": True,
                    "backendImage": "invalid.example.test/never-started:latest",
                    "jwkUrl": "https://invalid.example.test/jwk",
                    "apiUrl": "https://invalid.example.test/api",
                }
            },
        ),
    ],
)
def test_invalid_internal_creation_has_no_workload(kubectl, cluster_extra, extra):
    name = "clusterip-invalid"
    body = {
        "apiVersion": "cloud.crate.io/v1",
        "kind": "CrateDB",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": deepcopy(resource(kubectl, "cratedb", NAME)["spec"]),
    }
    body["spec"]["cluster"].update(cluster_extra)
    body["spec"].update(extra)
    kubectl("create", "-f", "-", body=body)
    try:
        uid = resource(kubectl, "cratedb", name)["metadata"]["uid"]
        wait_for(
            lambda: rejection_events(
                kubectl, uid, "cluster_create", "clusterip exposure does not support"
            ),
            "explicit permanent creation rejection",
            timeout=60,
        )
        for kind in ["services", "statefulsets", "deployments", "ingresses"]:
            items = json.loads(kubectl("get", kind, "-n", NAMESPACE, "-o", "json"))
            assert not any(
                owner.get("name") == name
                for item in items["items"]
                for owner in item["metadata"].get("ownerReferences", [])
            )
    finally:
        kubectl("delete", "cratedb", name, "-n", NAMESPACE, "--timeout=90s")


def test_internal_exposure_transition_is_rejected_without_service_change(kubectl):
    uid = assert_internal_service(kubectl)
    cr_uid = resource(kubectl, "cratedb", NAME)["metadata"]["uid"]
    reason = "Transitions to or from clusterip are not supported"
    previous_events = rejection_events(kubectl, cr_uid, "cluster_update", reason)
    try:
        kubectl(
            "patch",
            "cratedb",
            NAME,
            "-n",
            NAMESPACE,
            "--type=merge",
            "-p",
            '{"spec":{"cluster":{"exposure":"loadbalancer"}}}',
        )

        wait_for(
            lambda: rejection_events(kubectl, cr_uid, "cluster_update", reason)
            != previous_events,
            "explicit unsupported transition rejection",
            timeout=60,
        )
        assert assert_internal_service(kubectl) == uid
        assert_sql_from_operator(kubectl)
    finally:
        kubectl(
            "patch",
            "cratedb",
            NAME,
            "-n",
            NAMESPACE,
            "--type=merge",
            "-p",
            '{"spec":{"cluster":{"exposure":"clusterip"}}}',
        )


def test_internal_compute_restart_keeps_service_and_sql(kubectl):
    wait_for(lambda: update_finished(kubectl, 1), "settled previous update")
    old_uid = resource(kubectl, "pod", POD)["metadata"]["uid"]
    service_uid = assert_internal_service(kubectl)
    nodes = resource(kubectl, "cratedb", NAME)["spec"]["nodes"]
    cpu = nodes["data"][0]["resources"]["limits"]["cpu"]
    nodes["data"][0]["resources"]["limits"]["cpu"] = 3 if cpu == 2 else 2
    kubectl(
        "patch",
        "cratedb",
        NAME,
        "-n",
        NAMESPACE,
        "--type=merge",
        "-p",
        json.dumps({"spec": {"nodes": nodes}}),
    )

    def replaced():
        pod = json.loads(
            kubectl(
                "get",
                "pod",
                POD,
                "-n",
                NAMESPACE,
                "--ignore-not-found",
                "-o",
                "json",
            )
            or "{}"
        )
        return pod.get("metadata", {}).get("uid") not in {None, old_uid}

    wait_for(replaced, "operator-driven compute restart")
    kubectl(
        "wait",
        "--for=condition=Ready",
        f"pod/{POD}",
        "-n",
        NAMESPACE,
        "--timeout=180s",
    )
    wait_for(lambda: update_finished(kubectl, 1), "completed compute restart")
    assert assert_internal_service(kubectl) == service_uid
    assert_sql_from_operator(kubectl)


def test_loadbalancer_to_internal_transition_is_rejected(kubectl):
    name = "clusterip-transition"
    body = {
        "apiVersion": "cloud.crate.io/v1",
        "kind": "CrateDB",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": deepcopy(resource(kubectl, "cratedb", NAME)["spec"]),
    }
    body["spec"]["cluster"]["exposure"] = "loadbalancer"
    kubectl("create", "-f", "-", body=body)
    try:
        wait_for(
            lambda: resource(kubectl, "cratedb", name)
            .get("status", {})
            .get("cluster_create/bootstrap", {})
            .get("success")
            is True,
            "baseline LoadBalancer cluster bootstrap",
        )
        service = resource(kubectl, "service", f"crate-{name}")
        assert service["spec"]["type"] == "LoadBalancer"
        cr_uid = resource(kubectl, "cratedb", name)["metadata"]["uid"]
        kubectl(
            "patch",
            "cratedb",
            name,
            "-n",
            NAMESPACE,
            "--type=merge",
            "-p",
            '{"spec":{"cluster":{"exposure":"clusterip"}}}',
        )
        wait_for(
            lambda: rejection_events(
                kubectl,
                cr_uid,
                "cluster_update",
                "Transitions to or from clusterip are not supported",
            ),
            "LoadBalancer-to-ClusterIP rejection",
            timeout=60,
        )
        current = resource(kubectl, "service", f"crate-{name}")
        assert current["metadata"]["uid"] == service["metadata"]["uid"]
        assert current["spec"]["type"] == "LoadBalancer"
    finally:
        kubectl("delete", "cratedb", name, "-n", NAMESPACE, "--timeout=90s")
        kubectl(
            "delete",
            "pvc",
            f"data0-crate-data-hot-{name}-0",
            "-n",
            NAMESPACE,
            "--ignore-not-found",
            "--timeout=90s",
        )
