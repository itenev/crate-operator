import datetime
import logging
from contextlib import ExitStack
from unittest import mock

import kopf
import pytest
from kubernetes_asyncio.client import V1ObjectMeta, V1StatefulSet, V1StatefulSetSpec

from crate.operator.config import config
from crate.operator.constants import CloudProvider
from crate.operator.create import get_data_service, recreate_services
from crate.operator.handlers.handle_create_cratedb import create_cratedb
from crate.operator.handlers.handle_create_grand_central import create_grand_central
from crate.operator.handlers.handle_update_cratedb import update_cratedb
from crate.operator.operations import suspend_or_start_cluster

LOGGER = logging.getLogger(__name__)


def internal_spec():
    return {
        "cluster": {
            "exposure": "clusterip",
            "name": "test",
            "imageRegistry": "crate",
            "version": "6.2.11",
        },
        "nodes": {"data": [{"name": "hot", "replicas": 1}]},
    }


async def test_internal_creation_registers_services_but_no_ingress():
    patch = kopf.Patch()
    with mock.patch("kopf.register") as register:
        await create_cratedb(
            "test",
            {"name": "test", "uid": "uid"},
            internal_spec(),
            patch,
            {},
            LOGGER,
        )
    handlers = [call.kwargs["id"] for call in register.call_args_list]
    assert "services" in handlers
    assert "bootstrap" in handlers
    assert "traefik_resources" not in handlers
    assert "cluster_create" in patch.status


@pytest.mark.parametrize(
    "exposure,service_type",
    [
        ("loadbalancer", "LoadBalancer"),
        ("traefik", "ClusterIP"),
        ("clusterip", "ClusterIP"),
    ],
)
def test_data_service_exposure(exposure, service_type):
    with mock.patch.object(config, "CLOUD_PROVIDER", CloudProvider.AWS):
        service = get_data_service(
            None, "test", {}, 4200, 5432, None, exposure=exposure
        )
    assert service.spec.type == service_type
    assert [(p.port, p.target_port) for p in service.spec.ports] == [
        (4200, 4200),
        (5432, 5432),
    ]
    assert service.spec.selector["app.kubernetes.io/name"] == "test"
    if exposure == "clusterip":
        assert service.metadata.annotations == {}
        assert service.spec.external_traffic_policy is None
        assert service.spec.load_balancer_source_ranges is None


async def test_recreated_internal_services_do_not_request_external_routing():
    services = []

    async def record_service(**kwargs):
        services.append(kwargs["body"])

    core = mock.Mock(
        create_namespaced_service=mock.AsyncMock(side_effect=record_service)
    )
    with (
        mock.patch("crate.operator.create.GlobalApiClient"),
        mock.patch("crate.operator.create.CoreV1Api", return_value=core),
        mock.patch.object(config, "CLOUD_PROVIDER", CloudProvider.AWS),
    ):
        await recreate_services(
            "test",
            "test",
            {"cluster": {"exposure": "clusterip"}, "nodes": {}},
            {"uid": "test-uid"},
            LOGGER,
        )
    assert [s.metadata.name for s in services] == ["crate-test", "crate-discovery-test"]
    assert services[0].spec.type == "ClusterIP"
    assert services[0].metadata.annotations == {}
    assert services[1].spec.cluster_ip == "None"


@pytest.mark.parametrize(
    "extra",
    [
        {"grandCentral": {"backendEnabled": True}},
        {"cluster": {"externalDNS": "db.example.test"}},
        {
            "cluster": {
                "service": {
                    "annotations": {
                        "external-dns.alpha.kubernetes.io/hostname": "db.example.test"
                    }
                }
            }
        },
        {
            "cluster": {
                "service": {
                    "annotations": {
                        "service.beta.kubernetes.io/aws-load-balancer-type": "nlb"
                    }
                }
            }
        },
    ],
)
async def test_invalid_internal_creation_fails_before_registration(extra):
    spec = internal_spec()
    for key, value in extra.items():
        spec.setdefault(key, {}).update(value)
    with mock.patch("kopf.register") as register:
        with pytest.raises(kopf.PermanentError, match="clusterip"):
            await create_cratedb(
                "test", {"name": "test", "uid": "uid"}, spec, kopf.Patch(), {}, LOGGER
            )
    register.assert_not_called()


@pytest.mark.parametrize(
    "cluster_updates",
    [
        {"externalDNS": "db.example.test"},
        {
            "service": {
                "annotations": {
                    "external-dns.alpha.kubernetes.io/hostname": "db.example.test"
                }
            }
        },
        {
            "service": {
                "annotations": {
                    "service.beta.kubernetes.io/aws-load-balancer-type": "nlb"
                }
            }
        },
    ],
)
async def test_invalid_internal_update_fails_before_registration(cluster_updates):
    spec = internal_spec()
    spec["cluster"].update(cluster_updates)
    body = kopf.Body({"metadata": {"name": "test", "uid": "uid"}, "spec": spec})
    diff = kopf.Diff(
        [
            kopf.DiffItem(
                kopf.DiffOperation.ADD,
                ("spec", "cluster", "externalDNS"),
                None,
                "db.example.test",
            )
        ]
    )
    with mock.patch("kopf.register") as register:
        with pytest.raises(kopf.PermanentError, match="clusterip"):
            await update_cratedb(
                "test",
                "test",
                kopf.Patch(),
                body,
                {},
                diff,
                datetime.datetime.now(datetime.timezone.utc),
                LOGGER,
            )
    register.assert_not_called()


@pytest.mark.parametrize(
    "old,new",
    [
        ("loadbalancer", "clusterip"),
        ("traefik", "clusterip"),
        ("clusterip", "loadbalancer"),
        ("clusterip", "traefik"),
        (None, "clusterip"),
        ("clusterip", None),
    ],
)
async def test_internal_transition_fails_before_update_registration(old, new):
    diff = kopf.Diff(
        [
            kopf.DiffItem(
                kopf.DiffOperation.CHANGE, ("spec", "cluster", "exposure"), old, new
            )
        ]
    )
    spec = internal_spec()
    spec["cluster"]["exposure"] = new or "loadbalancer"
    body = kopf.Body({"metadata": {"name": "test", "uid": "uid"}, "spec": spec})
    with mock.patch("kopf.register") as register:
        with pytest.raises(kopf.PermanentError, match="clusterip"):
            await update_cratedb(
                "test",
                "test",
                kopf.Patch(),
                body,
                {},
                diff,
                datetime.datetime.now(datetime.timezone.utc),
                LOGGER,
            )
    register.assert_not_called()


async def test_enabling_grand_central_later_on_internal_cluster_is_rejected():
    cratedb = {
        "metadata": {"name": "test", "uid": "uid"},
        "spec": {
            "cluster": {"exposure": "clusterip"},
            "grandCentral": {"backendEnabled": True},
        },
    }
    with (
        mock.patch(
            "crate.operator.handlers.handle_create_grand_central.get_cratedb_resource",
            new=mock.AsyncMock(return_value=cratedb),
        ),
        mock.patch("kopf.register") as register,
    ):
        with pytest.raises(kopf.PermanentError, match="clusterip"):
            await create_grand_central(
                "test", "test", None, {"backendEnabled": True}, LOGGER
            )
    register.assert_not_called()


async def test_internal_resume_does_not_wait_for_loadbalancer_or_traefik():
    cratedb = {
        "spec": {
            "cluster": {"exposure": "clusterip"},
            "nodes": {"data": [{"name": "hot", "replicas": 1}]},
        },
        "metadata": {"uid": "test-uid"},
    }
    sts = V1StatefulSet(
        metadata=V1ObjectMeta(name="crate-data-hot-test"),
        spec=V1StatefulSetSpec(
            replicas=0, selector={}, service_name="test", template={}
        ),
    )
    apps = mock.Mock(read_namespaced_stateful_set=mock.AsyncMock(return_value=sts))
    core = mock.Mock()
    diff = kopf.Diff(
        [kopf.DiffItem(kopf.DiffOperation.CHANGE, ("0", "replicas"), 0, 1)]
    )
    with ExitStack() as stack:
        module = "crate.operator.operations."
        stack.enter_context(
            mock.patch(
                module + "get_cratedb_resource",
                new=mock.AsyncMock(return_value=cratedb),
            )
        )
        stack.enter_context(
            mock.patch(
                module + "is_service_present", new=mock.AsyncMock(return_value=True)
            )
        )
        for method in ("is_lb_service_ready", "are_traefik_resources_present"):
            stack.enter_context(
                mock.patch(
                    module + method,
                    new=mock.AsyncMock(
                        side_effect=AssertionError("external routing queried")
                    ),
                )
            )
        patches = {}
        for method in (
            "update_statefulset_replicas",
            "suspend_or_start_grand_central",
            "send_operation_progress_notification",
            "_get_connection_factory",
            "check_all_data_nodes_present",
        ):
            patches[method] = stack.enter_context(
                mock.patch(module + method, new=mock.AsyncMock())
            )
        await suspend_or_start_cluster(
            apps, core, "test", "test", cratedb, diff, False, LOGGER
        )
        patches["update_statefulset_replicas"].assert_awaited_once()


async def test_internal_suspend_removes_client_service_without_ingress_api():
    cratedb = {"spec": internal_spec(), "metadata": {"uid": "uid"}}
    sts = V1StatefulSet(
        metadata=V1ObjectMeta(name="crate-data-hot-test"),
        spec=V1StatefulSetSpec(
            replicas=1, selector={}, service_name="test", template={}
        ),
    )
    apps = mock.Mock(read_namespaced_stateful_set=mock.AsyncMock(return_value=sts))
    core = mock.Mock(delete_namespaced_service=mock.AsyncMock())
    diff = kopf.Diff(
        [kopf.DiffItem(kopf.DiffOperation.CHANGE, ("0", "replicas"), 1, 0)]
    )
    with ExitStack() as stack:
        module = "crate.operator.operations."
        stack.enter_context(
            mock.patch(
                module + "get_cratedb_resource",
                new=mock.AsyncMock(return_value=cratedb),
            )
        )
        stack.enter_context(
            mock.patch(
                module + "delete_traefik_resources",
                new=mock.AsyncMock(
                    side_effect=AssertionError("ingress deletion queried")
                ),
            )
        )
        for method in (
            "_get_connection_factory",
            "check_cluster_healthy",
            "update_statefulset_replicas",
            "suspend_or_start_grand_central",
            "send_operation_progress_notification",
            "check_all_data_nodes_gone",
        ):
            stack.enter_context(mock.patch(module + method, new=mock.AsyncMock()))
        await suspend_or_start_cluster(
            apps, core, "test", "test", cratedb, diff, False, LOGGER
        )
    core.delete_namespaced_service.assert_awaited_once_with(
        name="crate-test", namespace="test"
    )
