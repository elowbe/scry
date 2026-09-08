"""Create a local TLS identity so browser input APIs run in a secure context."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path

from .config import AppConfig


def ensure_certificate(config: AppConfig, *, force: bool = False) -> tuple[Path, Path]:
    cert_path, key_path = config.server.cert_file, config.server.key_file
    if cert_path.is_file() and key_path.is_file() and not force:
        return cert_path, key_path
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError("cryptography is required; install the project dependencies first") from exc

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    hostname = config.server.public_host or socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(hostname)))
    except ValueError:
        names.append(x509.DNSName(hostname))
    for address in ("127.0.0.1", "::1"):
        names.append(x509.IPAddress(ipaddress.ip_address(address)))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key_bytes)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o644)
    return cert_path, key_path

