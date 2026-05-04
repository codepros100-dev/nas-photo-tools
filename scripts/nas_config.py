"""Shared NAS configuration loaded from environment variables.

Environment variables (with sensible defaults for local LAN NAS setups):
  NAS_HOST            NAS IP/hostname              (required)
  NAS_USER            SMB user                     (required)
  NAS_PASS            SMB password                 (required)
  NAS_SHARE           Share name                   (default: 'Photos')
  NAS_CLIENT_NAME     This client's NetBIOS name   (default: 'CLIENT')
  NAS_SERVER_NAME     NAS NetBIOS name             (default: 'NAS')
  NAS_USE_NTLMV2      'true' or 'false'            (default: 'true')
  NAS_DIRECT_TCP      'true' (port 445) / 'false'  (default: 'true')

  PHOTO_LIBRARY_ROOT  local mount point for browsing  (e.g. 'P:/Library')
  PHOTO_DB_DIR        directory for state files       (default: ~/.nas-photo-tools)

Either set them in your environment, or drop a `.env` file next to this script
with KEY=value lines (no quoting). The latter is convenient for Windows users
who don't want to fiddle with system env vars.
"""
import os
from pathlib import Path


def _load_dotenv():
    """Load .env file if present (next to this script or current dir)."""
    candidates = [
        Path.cwd() / '.env',
        Path(__file__).parent / '.env',
        Path.home() / '.nas-photo-tools' / '.env',
    ]
    for env_file in candidates:
        if env_file.exists():
            for raw in env_file.read_text(encoding='utf-8').splitlines():
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
            return env_file
    return None


_load_dotenv()


def _bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ('1', 'true', 'yes', 'y', 'on')


def _required(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(
            f'{name} is not set. Define it in environment or in a .env file. '
            f'See {__file__} for the full list.'
        )
    return v


# --- SMB ---
NAS_HOST = os.environ.get('NAS_HOST', '')
NAS_USER = os.environ.get('NAS_USER', '')
NAS_PASS = os.environ.get('NAS_PASS', '')
NAS_SHARE = os.environ.get('NAS_SHARE', 'Photos')
NAS_CLIENT_NAME = os.environ.get('NAS_CLIENT_NAME', 'CLIENT')
NAS_SERVER_NAME = os.environ.get('NAS_SERVER_NAME', 'NAS')
NAS_USE_NTLMV2 = _bool('NAS_USE_NTLMV2', True)
NAS_DIRECT_TCP = _bool('NAS_DIRECT_TCP', True)
NAS_PORT = 445 if NAS_DIRECT_TCP else 139

# --- Local paths ---
PHOTO_LIBRARY_ROOT = Path(os.environ.get(
    'PHOTO_LIBRARY_ROOT', str(Path.home() / 'PhotoLibrary')))
PHOTO_DB_DIR = Path(os.environ.get(
    'PHOTO_DB_DIR', str(Path.home() / '.nas-photo-tools')))

PHOTO_DB_DIR.mkdir(parents=True, exist_ok=True)


def smb_connect(timeout=60):
    """Open an SMB connection using configured credentials. Requires pysmb."""
    from smb.SMBConnection import SMBConnection
    NAS_HOST_R = _required('NAS_HOST')
    conn = SMBConnection(
        _required('NAS_USER'),
        _required('NAS_PASS'),
        NAS_CLIENT_NAME,
        NAS_SERVER_NAME,
        use_ntlm_v2=NAS_USE_NTLMV2,
        is_direct_tcp=NAS_DIRECT_TCP,
    )
    conn.connect(NAS_HOST_R, NAS_PORT, timeout=timeout)
    return conn
