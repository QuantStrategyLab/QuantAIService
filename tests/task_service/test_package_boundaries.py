"""HTTP transport vs application package boundary (untangle wave3)."""

from __future__ import annotations

from ai_service import application as application_pkg
from ai_service import server as server_pkg
from ai_service.application import Application as ApplicationFromPackage
from ai_service.server import Application as ApplicationFromServer


def test_application_reexport_is_identical_object():
    assert ApplicationFromServer is ApplicationFromPackage
    assert server_pkg.Application is application_pkg.Application


def test_server_exposes_transport_entrypoints():
    assert callable(server_pkg.make_server)
    assert callable(server_pkg.main)
    assert callable(server_pkg.build_application)
