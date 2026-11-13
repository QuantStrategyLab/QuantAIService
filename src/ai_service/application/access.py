"""Authentication, scopes, and project admission for the task application."""
from __future__ import annotations

from dataclasses import replace

from ..auth import ProjectAdmissionError


class ApplicationAccessMixin:
    @staticmethod
    def require(principal, scope):
        if scope not in principal.scopes:
            raise PermissionError('required scope not granted')

    def authenticate(self, headers):
        principal = self.auth.authenticate(headers.get("Authorization"))
        if not any(principal.allows_project(project) for project in self.runtime.policies):
            raise ProjectAdmissionError(principal)
        return principal

    def project_context(self, principal, project=None):
        selected = principal.project if project is None else project
        if not isinstance(selected, str) or not principal.allows_project(selected):
            raise PermissionError('an authorized project must be selected')
        if selected not in self.runtime.policies:
            raise PermissionError('project admission revoked')
        return replace(principal, project=selected)
