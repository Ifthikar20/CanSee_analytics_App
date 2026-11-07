"""P0.7: EncryptedTextField must fail closed, not silently store plaintext."""
import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from core.checks import check_field_encryption_key
from core.encryption import field_encryption
from core.encryption.field_encryption import _MARKER, EncryptedTextField

_KEY = Fernet.generate_key().decode()


def _field():
    return EncryptedTextField()


@override_settings(FIELD_ENCRYPTION_KEY=_KEY, FIELD_ENCRYPTION_REQUIRED=True)
def test_round_trip_with_key():
    f = _field()
    stored = f.get_prep_value("s3cret-token")
    assert stored.startswith(_MARKER)          # marked as encrypted
    assert "s3cret-token" not in stored        # not plaintext
    assert f.from_db_value(stored, None, None) == "s3cret-token"


@override_settings(FIELD_ENCRYPTION_KEY="", FIELD_ENCRYPTION_REQUIRED=True)
def test_required_but_missing_raises_on_write():
    with pytest.raises(ImproperlyConfigured):
        _field().get_prep_value("secret")


@override_settings(FIELD_ENCRYPTION_KEY="", FIELD_ENCRYPTION_REQUIRED=True)
def test_required_but_missing_raises_on_read_of_encrypted():
    with pytest.raises(ImproperlyConfigured):
        _field().from_db_value(_MARKER + "anything", None, None)


@override_settings(FIELD_ENCRYPTION_KEY=_KEY, FIELD_ENCRYPTION_REQUIRED=True)
def test_tampered_ciphertext_raises_not_passthrough():
    with pytest.raises(ImproperlyConfigured):
        _field().from_db_value(_MARKER + "not-a-valid-token", None, None)


@override_settings(FIELD_ENCRYPTION_KEY=_KEY, FIELD_ENCRYPTION_REQUIRED=True)
def test_legacy_plaintext_without_marker_read_as_is():
    # Rows written before encryption was enabled have no marker.
    assert _field().from_db_value("legacy-plaintext", None, None) == "legacy-plaintext"


@override_settings(FIELD_ENCRYPTION_KEY="", FIELD_ENCRYPTION_REQUIRED=False)
def test_dev_without_key_passes_through_plaintext_and_warns(caplog):
    field_encryption._warned_no_key = False  # reset one-time guard
    f = _field()
    assert f.get_prep_value("dev-value") == "dev-value"
    assert any("PLAINTEXT" in r.message for r in caplog.records)


class TestSystemCheck:
    @override_settings(FIELD_ENCRYPTION_REQUIRED=False, FIELD_ENCRYPTION_KEY="")
    def test_not_required_no_errors(self):
        assert check_field_encryption_key(None) == []

    @override_settings(FIELD_ENCRYPTION_REQUIRED=True, FIELD_ENCRYPTION_KEY="")
    def test_required_missing_key_errors(self):
        errors = check_field_encryption_key(None)
        assert [e.id for e in errors] == ["core.E001"]

    @override_settings(FIELD_ENCRYPTION_REQUIRED=True, FIELD_ENCRYPTION_KEY="not-a-fernet-key")
    def test_required_invalid_key_errors(self):
        errors = check_field_encryption_key(None)
        assert [e.id for e in errors] == ["core.E002"]

    @override_settings(FIELD_ENCRYPTION_REQUIRED=True, FIELD_ENCRYPTION_KEY=_KEY)
    def test_required_valid_key_no_errors(self):
        assert check_field_encryption_key(None) == []
