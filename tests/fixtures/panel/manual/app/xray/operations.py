from functools import lru_cache
from copy import deepcopy
from threading import Lock, Thread
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from app import logger, xray
from app.db import GetDB, crud
from app.models.node import NodeStatus
from app.models.user import UserResponse
from app.utils.concurrency import threaded_function
from app.xray.node import XRayNode
from xray_api import XRay as XRayAPI
from xray_api.types.account import Account, XTLSFlows

if TYPE_CHECKING:
    from app.db import User as DBUser
    from app.db.models import Node as DBNode


@lru_cache(maxsize=None)
def get_tls():
    from app.db import GetDB, get_tls_certificate
    with GetDB() as db:
        tls = get_tls_certificate(db)
        return {
            "key": tls.key,
            "certificate": tls.certificate
        }


@threaded_function
def _add_user_to_inbound(api: XRayAPI, inbound_tag: str, account: Account):
    try:
        api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
    except (
        xray.exc.EmailExistsError,
        xray.exc.ConnectionError,
        xray.exc.TagNotFoundError,
        xray.exc.TimeoutError,
    ):
        pass


@threaded_function
def _remove_user_from_inbound(api: XRayAPI, inbound_tag: str, email: str):
    try:
        api.remove_inbound_user(tag=inbound_tag, email=email, timeout=30)
    except (
        xray.exc.EmailNotFoundError,
        xray.exc.ConnectionError,
        xray.exc.TagNotFoundError,
        xray.exc.TimeoutError,
    ):
        pass


@threaded_function
def _alter_inbound_user(api: XRayAPI, inbound_tag: str, account: Account):
    try:
        api.remove_inbound_user(tag=inbound_tag, email=account.email, timeout=30)
    except (
        xray.exc.EmailNotFoundError,
        xray.exc.ConnectionError,
        xray.exc.TagNotFoundError,
        xray.exc.TimeoutError,
    ):
        pass
    try:
        api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
    except (
        xray.exc.EmailExistsError,
        xray.exc.ConnectionError,
        xray.exc.TagNotFoundError,
        xray.exc.TimeoutError,
    ):
        pass


def add_user(dbuser: "DBUser"):
    user = UserResponse.model_validate(dbuser)
    email = f"{dbuser.id}.{dbuser.username}"

    for proxy_type, inbound_tags in user.inbounds.items():
        for inbound_tag in inbound_tags:
            inbound = xray.config.inbounds_by_tag.get(inbound_tag, {})

            try:
                proxy_settings = user.proxies[proxy_type].dict(no_obj=True)
            except KeyError:
                pass
            account = proxy_type.account_model(email=email, **proxy_settings)

            # XTLS currently only supports transmission methods of TCP and mKCP
            if getattr(account, 'flow', None) and (
                inbound.get('network', 'tcp') not in ('tcp', 'raw', 'kcp')
                or
                (
                    inbound.get('network', 'tcp') in ('tcp', 'raw', 'kcp')
                    and
                    inbound.get('tls') not in ('tls', 'reality')
                )
                or
                inbound.get('header_type') == 'http'
            ):
                account.flow = XTLSFlows.NONE

            _add_user_to_inbound(xray.api, inbound_tag, account)  # main core
            for node in list(xray.nodes.values()):
                if node.connected and node.started:
                    _add_user_to_inbound(node.api, inbound_tag, account)


def remove_user(dbuser: "DBUser"):
    email = f"{dbuser.id}.{dbuser.username}"

    for inbound_tag in xray.config.inbounds_by_tag:
        _remove_user_from_inbound(xray.api, inbound_tag, email)
        for node in list(xray.nodes.values()):
            if node.connected and node.started:
                _remove_user_from_inbound(node.api, inbound_tag, email)


def update_user(dbuser: "DBUser"):
    user = UserResponse.model_validate(dbuser)
    email = f"{dbuser.id}.{dbuser.username}"

    active_inbounds = []
    for proxy_type, inbound_tags in user.inbounds.items():
        for inbound_tag in inbound_tags:
            active_inbounds.append(inbound_tag)
            inbound = xray.config.inbounds_by_tag.get(inbound_tag, {})

            try:
                proxy_settings = user.proxies[proxy_type].dict(no_obj=True)
            except KeyError:
                pass
            account = proxy_type.account_model(email=email, **proxy_settings)

            # XTLS currently only supports transmission methods of TCP and mKCP
            if getattr(account, 'flow', None) and (
                inbound.get('network', 'tcp') not in ('tcp', 'kcp')
                or
                (
                    inbound.get('network', 'tcp') in ('tcp', 'kcp')
                    and
                    inbound.get('tls') not in ('tls', 'reality')
                )
                or
                inbound.get('header_type') == 'http'
            ):
                account.flow = XTLSFlows.NONE

            _alter_inbound_user(xray.api, inbound_tag, account)  # main core
            for node in list(xray.nodes.values()):
                if node.connected and node.started:
                    _alter_inbound_user(node.api, inbound_tag, account)

    for inbound_tag in xray.config.inbounds_by_tag:
        if inbound_tag in active_inbounds:
            continue
        # remove disabled inbounds
        _remove_user_from_inbound(xray.api, inbound_tag, email)
        for node in list(xray.nodes.values()):
            if node.connected and node.started:
                _remove_user_from_inbound(node.api, inbound_tag, email)


def remove_node(node_id: int):
    if node_id in xray.nodes:
        try:
            xray.nodes[node_id].disconnect()
        except Exception:
            pass
        finally:
            try:
                del xray.nodes[node_id]
            except KeyError:
                pass


def add_node(dbnode: "DBNode"):
    remove_node(dbnode.id)

    tls = get_tls()
    xray.nodes[dbnode.id] = XRayNode(address=dbnode.address,
                                     port=dbnode.port,
                                     api_port=dbnode.api_port,
                                     ssl_key=tls['key'],
                                     ssl_cert=tls['certificate'],
                                     usage_coefficient=dbnode.usage_coefficient)

    return xray.nodes[dbnode.id]


def _change_node_status(node_id: int, status: NodeStatus, message: str = None, version: str = None):
    with GetDB() as db:
        try:
            dbnode = crud.get_node_by_id(db, node_id)
            if not dbnode:
                return

            if dbnode.status == NodeStatus.disabled:
                remove_node(dbnode.id)
                return

            crud.update_node_status(db, dbnode, status, message, version)
        except SQLAlchemyError:
            db.rollback()


# Register before starting a worker, so health ticks cannot spawn overlapping work.
_node_operations_lock = Lock()
_node_operations = {}


def get_node_ids():
    # An admitted worker may not have inserted its new runtime object yet.
    with _node_operations_lock:
        return sorted(set(xray.nodes) | set(_node_operations))


def _request_node_operation(node_id, action, config, health_check):
    with _node_operations_lock:
        if health_check and node_id in _node_operations:
            return

    # Preserve the exact requested config without sharing TLS preparation mutations.
    request = (action, deepcopy(config) if config is not None else None)
    with _node_operations_lock:
        operation = _node_operations.get(node_id)
        if operation is not None:
            if not health_check:
                operation["pending"] = request
            return
        operation = {"pending": request}
        _node_operations[node_id] = operation
        worker = Thread(target=_run_node_operations, args=(node_id, operation), daemon=True)
        try:
            worker.start()
        except Exception:
            del _node_operations[node_id]
            raise


def _run_node_operations(node_id, operation):
    try:
        while True:
            with _node_operations_lock:
                action, config = operation["pending"]
                operation["pending"] = None
            try:
                _perform_node_operation(node_id, action, config)
            except Exception:
                # Also recover from failures before a node/DB object can be obtained.
                logger.exception("Node %s lifecycle worker failed", node_id)
            with _node_operations_lock:
                if operation["pending"] is None:
                    if _node_operations.get(node_id) is operation:
                        del _node_operations[node_id]
                    return
    finally:
        with _node_operations_lock:
            if _node_operations.get(node_id) is operation:
                del _node_operations[node_id]


def _perform_node_operation(node_id, action, config):
    with GetDB() as db:
        dbnode = crud.get_node_by_id(db, node_id)
    if not dbnode or dbnode.status == NodeStatus.disabled:
        return

    node = None
    try:
        node = xray.nodes.get(node_id)
        if node is None or not node.connected:
            node = add_node(dbnode)
            action = "connect"

        if config is None:
            config = xray.config.include_db_users()

        if action == "connect":
            _change_node_status(node_id, NodeStatus.connecting)
            logger.info("Connecting to node %s (%s)", node_id, dbnode.name)
            node.start(config)
            version = node.get_version()
        else:
            logger.info("Restarting Xray core of node %s (%s)", node_id, dbnode.name)
            node.restart(config)
            version = dbnode.xray_version

        if xray.nodes.get(node_id) is node:
            _change_node_status(node_id, NodeStatus.connected, version=version)
            logger.info("Node %s lifecycle operation %s completed", node_id, action)
    except Exception as exc:
        # A removed/replaced node is no longer owned by this operation.
        if node is not None and xray.nodes.get(node_id) is node:
            _change_node_status(node_id, NodeStatus.error, message=str(exc))
            logger.exception("Node %s lifecycle operation %s failed", node_id, action)
            try:
                node.disconnect()
            except Exception:
                logger.exception("Node %s cleanup after %s failed", node_id, action)


def connect_node(node_id, config=None, *, health_check=False):
    _request_node_operation(node_id, "connect", config, health_check)


def restart_node(node_id, config=None, *, health_check=False):
    _request_node_operation(node_id, "restart", config, health_check)


__all__ = [
    "add_user",
    "remove_user",
    "add_node",
    "remove_node",
    "connect_node",
    "restart_node",
]
