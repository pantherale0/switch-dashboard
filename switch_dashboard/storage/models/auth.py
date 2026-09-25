from sqlalchemy import Column, Float, String, Text

from switch_dashboard.storage.models.base import Base


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash = Column(String(64), primary_key=True)
    subject = Column(String(255), nullable=False, index=True)
    username = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False)
    csrf_token = Column(String(64), nullable=False)
    created_at = Column(Float, nullable=False)
    last_seen_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False, index=True)


class OidcLoginTransaction(Base):
    __tablename__ = "oidc_login_transactions"

    state_hash = Column(String(64), primary_key=True)
    browser_token_hash = Column(String(64), nullable=False)
    nonce = Column(String(128), nullable=False)
    code_verifier = Column(String(128), nullable=False)
    return_to = Column(Text, nullable=False, default="/")
    expires_at = Column(Float, nullable=False, index=True)


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_events"

    id = Column(String(36), primary_key=True)
    created_at = Column(Float, nullable=False, index=True)
    subject = Column(String(255), nullable=False, default="anonymous")
    username = Column(String(255), nullable=False, default="anonymous")
    action = Column(String(255), nullable=False)
    target = Column(Text, nullable=False, default="")
    result = Column(String(32), nullable=False)
    source_ip = Column(String(64), nullable=False, default="")
