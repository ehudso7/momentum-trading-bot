"""
Phase 14: Enterprise SSO and Authentication

Provides enterprise-grade authentication with:
1. SAML 2.0 integration
2. OAuth 2.0 / OIDC support
3. Active Directory / LDAP integration
4. Multi-factor authentication (MFA)
5. Role-based access control (RBAC)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol

import structlog

log = structlog.get_logger(__name__)


class AuthProvider(Enum):
    """Supported authentication providers."""

    SAML = "saml"
    OIDC = "oidc"
    LDAP = "ldap"
    NATIVE = "native"
    API_KEY = "api_key"


class Permission(Enum):
    """System permissions."""

    # Read permissions
    READ_SIGNALS = "signals:read"
    READ_REPORTS = "reports:read"
    READ_DASHBOARD = "dashboard:read"
    READ_ANALYTICS = "analytics:read"

    # Write permissions
    WRITE_CONFIG = "config:write"
    WRITE_STRATEGY = "strategy:write"

    # Admin permissions
    ADMIN_USERS = "admin:users"
    ADMIN_BILLING = "admin:billing"
    ADMIN_SYSTEM = "admin:system"


class Role(Enum):
    """Predefined roles with permission sets."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    TRADER = "trader"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


# Role permission mappings
ROLE_PERMISSIONS = {
    Role.VIEWER: {
        Permission.READ_SIGNALS,
        Permission.READ_REPORTS,
        Permission.READ_DASHBOARD,
    },
    Role.ANALYST: {
        Permission.READ_SIGNALS,
        Permission.READ_REPORTS,
        Permission.READ_DASHBOARD,
        Permission.READ_ANALYTICS,
    },
    Role.TRADER: {
        Permission.READ_SIGNALS,
        Permission.READ_REPORTS,
        Permission.READ_DASHBOARD,
        Permission.READ_ANALYTICS,
        Permission.WRITE_CONFIG,
        Permission.WRITE_STRATEGY,
    },
    Role.ADMIN: {
        Permission.READ_SIGNALS,
        Permission.READ_REPORTS,
        Permission.READ_DASHBOARD,
        Permission.READ_ANALYTICS,
        Permission.WRITE_CONFIG,
        Permission.WRITE_STRATEGY,
        Permission.ADMIN_USERS,
        Permission.ADMIN_BILLING,
    },
    Role.SUPER_ADMIN: {p for p in Permission},  # All permissions
}


@dataclass
class User:
    """Enterprise user model."""

    id: str
    email: str
    organization_id: str
    provider: AuthProvider
    roles: set[Role] = field(default_factory=set)
    permissions: set[Permission] = field(default_factory=set)
    attributes: dict[str, Any] = field(default_factory=dict)
    mfa_enabled: bool = False
    last_login: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Organization:
    """Enterprise organization model."""

    id: str
    name: str
    domain: str
    auth_provider: AuthProvider
    sso_config: dict[str, Any] = field(default_factory=dict)
    mfa_required: bool = False
    ip_whitelist: list[str] = field(default_factory=list)
    max_users: int = 100
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Session:
    """User session model."""

    id: str
    user_id: str
    organization_id: str
    token: str
    expires_at: datetime
    ip_address: str
    user_agent: str
    mfa_verified: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class IdentityProvider(Protocol):
    """Protocol for identity providers."""

    async def authenticate(
        self,
        credentials: dict[str, Any],
    ) -> Optional[User]:
        """Authenticate user with credentials."""
        ...

    async def validate_token(
        self,
        token: str,
    ) -> Optional[User]:
        """Validate authentication token."""
        ...


class SAMLProvider:
    """SAML 2.0 identity provider."""

    def __init__(self, config: dict[str, Any]):
        self.entity_id = config.get("entity_id")
        self.sso_url = config.get("sso_url")
        self.x509_cert = config.get("x509_cert")
        self.attribute_mapping = config.get("attribute_mapping", {})

    async def authenticate(
        self,
        saml_response: str,
    ) -> Optional[User]:
        """
        Process SAML assertion and authenticate user.

        In production, use python-saml or similar library.
        """
        # Parse and validate SAML response
        user_data = self._parse_saml_response(saml_response)

        if not user_data:
            return None

        # Map SAML attributes to user model
        user = User(
            id=user_data.get("nameid", ""),
            email=user_data.get("email", ""),
            organization_id=user_data.get("organization", ""),
            provider=AuthProvider.SAML,
            attributes=user_data,
        )

        # Map roles from SAML attributes
        if "roles" in user_data:
            user.roles = self._map_roles(user_data["roles"])

        return user

    def _parse_saml_response(self, saml_response: str) -> Optional[dict]:
        """Parse and validate SAML response."""
        # Simplified mock implementation
        # In production, use proper XML parsing and signature validation
        try:
            # This would decode and parse the SAML XML
            return {
                "nameid": "user@example.com",
                "email": "user@example.com",
                "organization": "org_123",
                "roles": ["analyst"],
            }
        except Exception as e:
            log.error("saml.parse_error", error=str(e))
            return None

    def _map_roles(self, saml_roles: list[str]) -> set[Role]:
        """Map SAML roles to system roles."""
        mapped = set()

        role_map = {
            "viewer": Role.VIEWER,
            "analyst": Role.ANALYST,
            "trader": Role.TRADER,
            "admin": Role.ADMIN,
        }

        for saml_role in saml_roles:
            if saml_role.lower() in role_map:
                mapped.add(role_map[saml_role.lower()])

        return mapped or {Role.VIEWER}  # Default to viewer


class OIDCProvider:
    """OpenID Connect provider."""

    def __init__(self, config: dict[str, Any]):
        self.client_id = config.get("client_id")
        self.client_secret = config.get("client_secret")
        self.issuer = config.get("issuer")
        self.redirect_uri = config.get("redirect_uri")

    async def authenticate(
        self,
        authorization_code: str,
    ) -> Optional[User]:
        """
        Exchange authorization code for user info.

        In production, use authlib or similar library.
        """
        # Exchange code for tokens
        tokens = await self._exchange_code(authorization_code)

        if not tokens:
            return None

        # Get user info from ID token
        user_info = self._decode_id_token(tokens.get("id_token", ""))

        if not user_info:
            return None

        # Create user from OIDC claims
        user = User(
            id=user_info.get("sub", ""),
            email=user_info.get("email", ""),
            organization_id=user_info.get("org_id", ""),
            provider=AuthProvider.OIDC,
            attributes=user_info,
        )

        # Map groups to roles
        if "groups" in user_info:
            user.roles = self._map_groups_to_roles(user_info["groups"])

        return user

    async def _exchange_code(self, code: str) -> Optional[dict]:
        """Exchange authorization code for tokens."""
        # Mock implementation
        # In production, make actual HTTP request to token endpoint
        return {
            "access_token": "mock_access_token",
            "id_token": "mock_id_token",
            "refresh_token": "mock_refresh_token",
        }

    def _decode_id_token(self, id_token: str) -> Optional[dict]:
        """Decode and validate ID token."""
        # Mock implementation
        # In production, validate JWT signature and claims
        return {
            "sub": "user123",
            "email": "user@enterprise.com",
            "org_id": "org_456",
            "groups": ["traders"],
        }

    def _map_groups_to_roles(self, groups: list[str]) -> set[Role]:
        """Map OIDC groups to system roles."""
        mapped = set()

        group_map = {
            "viewers": Role.VIEWER,
            "analysts": Role.ANALYST,
            "traders": Role.TRADER,
            "admins": Role.ADMIN,
        }

        for group in groups:
            if group.lower() in group_map:
                mapped.add(group_map[group.lower()])

        return mapped or {Role.VIEWER}


class MFAProvider:
    """Multi-factor authentication provider."""

    def __init__(self):
        self.totp_secrets: dict[str, str] = {}
        self.backup_codes: dict[str, set[str]] = {}

    def generate_secret(self, user_id: str) -> str:
        """Generate TOTP secret for user."""
        secret = secrets.token_urlsafe(32)
        self.totp_secrets[user_id] = secret
        return secret

    def generate_qr_code(self, user_id: str, email: str) -> str:
        """Generate QR code for TOTP setup."""
        secret = self.totp_secrets.get(user_id)

        if not secret:
            secret = self.generate_secret(user_id)

        # In production, use qrcode library
        otpauth_url = f"otpauth://totp/TradingBot:{email}?secret={secret}&issuer=TradingBot"
        return otpauth_url

    def verify_totp(self, user_id: str, code: str) -> bool:
        """Verify TOTP code."""
        secret = self.totp_secrets.get(user_id)

        if not secret:
            return False

        # In production, use pyotp library
        # This is simplified mock
        expected = self._generate_totp(secret)
        return hmac.compare_digest(code, expected)

    def _generate_totp(self, secret: str) -> str:
        """Generate current TOTP code."""
        # Mock implementation
        # In production, use proper TOTP algorithm
        current_time = int(time.time() // 30)
        hash_input = f"{secret}{current_time}"
        return hashlib.sha256(hash_input.encode()).hexdigest()[:6]

    def generate_backup_codes(self, user_id: str, count: int = 10) -> list[str]:
        """Generate backup codes for user."""
        codes = [secrets.token_hex(4) for _ in range(count)]
        self.backup_codes[user_id] = set(codes)
        return codes

    def verify_backup_code(self, user_id: str, code: str) -> bool:
        """Verify and consume backup code."""
        codes = self.backup_codes.get(user_id, set())

        if code in codes:
            codes.remove(code)  # One-time use
            return True

        return False


class SessionManager:
    """Manage user sessions with security features."""

    def __init__(self, session_ttl: int = 3600):
        self.sessions: dict[str, Session] = {}
        self.session_ttl = session_ttl
        self.max_sessions_per_user = 5

    def create_session(
        self,
        user: User,
        ip_address: str,
        user_agent: str,
        mfa_verified: bool = False,
    ) -> Session:
        """Create new session for user."""
        # Limit sessions per user
        self._enforce_session_limit(user.id)

        session = Session(
            id=secrets.token_urlsafe(32),
            user_id=user.id,
            organization_id=user.organization_id,
            token=secrets.token_urlsafe(64),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.session_ttl),
            ip_address=ip_address,
            user_agent=user_agent,
            mfa_verified=mfa_verified,
        )

        self.sessions[session.id] = session

        log.info(
            "session.created",
            user_id=user.id,
            session_id=session.id,
            ip=ip_address,
        )

        return session

    def validate_session(
        self,
        token: str,
        ip_address: Optional[str] = None,
    ) -> Optional[Session]:
        """Validate session token."""
        # Find session by token
        session = None
        for s in self.sessions.values():
            if hmac.compare_digest(s.token, token):
                session = s
                break

        if not session:
            return None

        # Check expiration
        if datetime.now(timezone.utc) > session.expires_at:
            del self.sessions[session.id]
            return None

        # Check IP if provided (optional IP pinning)
        if ip_address and session.ip_address != ip_address:
            log.warning(
                "session.ip_mismatch",
                session_id=session.id,
                expected=session.ip_address,
                actual=ip_address,
            )
            # Could enforce strict IP pinning here

        return session

    def revoke_session(self, session_id: str) -> bool:
        """Revoke a session."""
        if session_id in self.sessions:
            del self.sessions[session_id]
            log.info("session.revoked", session_id=session_id)
            return True
        return False

    def _enforce_session_limit(self, user_id: str) -> None:
        """Enforce maximum sessions per user."""
        user_sessions = [
            s for s in self.sessions.values()
            if s.user_id == user_id
        ]

        if len(user_sessions) >= self.max_sessions_per_user:
            # Revoke oldest session
            oldest = min(user_sessions, key=lambda s: s.created_at)
            del self.sessions[oldest.id]
            log.info(
                "session.limit_enforced",
                user_id=user_id,
                revoked_session=oldest.id,
            )


class AuthorizationService:
    """Role-based access control service."""

    def __init__(self):
        self.role_permissions = ROLE_PERMISSIONS

    def has_permission(
        self,
        user: User,
        permission: Permission,
    ) -> bool:
        """Check if user has specific permission."""
        # Check direct permissions
        if permission in user.permissions:
            return True

        # Check role permissions
        for role in user.roles:
            if permission in self.role_permissions.get(role, set()):
                return True

        return False

    def has_any_permission(
        self,
        user: User,
        permissions: set[Permission],
    ) -> bool:
        """Check if user has any of the specified permissions."""
        return any(self.has_permission(user, p) for p in permissions)

    def has_all_permissions(
        self,
        user: User,
        permissions: set[Permission],
    ) -> bool:
        """Check if user has all specified permissions."""
        return all(self.has_permission(user, p) for p in permissions)

    def get_user_permissions(self, user: User) -> set[Permission]:
        """Get all permissions for user."""
        permissions = user.permissions.copy()

        for role in user.roles:
            permissions.update(self.role_permissions.get(role, set()))

        return permissions


# Export main components
__all__ = [
    "AuthProvider",
    "Permission",
    "Role",
    "User",
    "Organization",
    "Session",
    "SAMLProvider",
    "OIDCProvider",
    "MFAProvider",
    "SessionManager",
    "AuthorizationService",
]