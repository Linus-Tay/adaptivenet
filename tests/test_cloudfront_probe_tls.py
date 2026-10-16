"""Regression for HTTPS trust used by the CloudFront probe."""
import ssl
from scripts.cloudfront_probe import verified_tls_context


def test_probe_keeps_certificate_verification_enabled():
    context = verified_tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
