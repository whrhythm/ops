import io
import os
from importlib.util import spec_from_file_location,module_from_spec
from pathlib import Path
import pytest
from cryptography.exceptions import InvalidTag

spec=spec_from_file_location('backup',Path(__file__).resolve().parents[1]/'scripts/local_backup_crypto.py')
b=module_from_spec(spec);spec.loader.exec_module(b)


def test_stream_roundtrip_and_authentication():
    key=os.urandom(32);content=os.urandom(b.CHUNK+19)
    output=io.BytesIO();expected=b.encrypt_stream(io.BytesIO(content),output,key,{'source':'test'})
    clear=io.BytesIO();actual=b.decrypt_stream(io.BytesIO(output.getvalue()),key,clear)
    assert clear.getvalue()==content
    assert actual['plaintext_sha256']==expected['plaintext_sha256']
    raw=bytearray(output.getvalue());raw[-30]^=1
    with pytest.raises(InvalidTag):b.decrypt_stream(io.BytesIO(raw),key)
    with pytest.raises(InvalidTag):b.decrypt_stream(io.BytesIO(output.getvalue()),os.urandom(32))
    with pytest.raises((InvalidTag,ValueError)):b.decrypt_stream(io.BytesIO(output.getvalue()[:-9]),key)
