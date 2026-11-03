"""Authenticator-app (TOTP, RFC 6238) helpers. Pure, no Flask or database.

The secret is stored encrypted with a key derived from the app's SECRET_KEY, so a leaked database or backup alone does
not give an attacker the second factor. Changing SECRET_KEY makes stored secrets unreadable (`unseal` returns None);
the portal then falls back to the emailed code and the admin enrols the app again.
"""
import base64
import hashlib
import hmac
import io
import time

import pyotp
import qrcode
import qrcode.image.svg
from cryptography.fernet import Fernet, InvalidToken

ISSUER = 'ORS SRS Portal'  # no slash: pyotp leaves "/" raw in the otpauth label path, which some apps misparse
STEP = 30  # seconds; fixed by the apps, so it cannot be tuned here


def _fernet(secret_key):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'totp:' + str(secret_key).encode()).digest()))


def new_secret():
    return pyotp.random_base32()


def seal(secret, secret_key):
    return _fernet(secret_key).encrypt(secret.encode()).decode()


def unseal(token, secret_key):
    """The plain secret, or None if it is missing or was sealed under another SECRET_KEY."""
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError, ValueError):
        return None


def provisioning_uri(secret, username):
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_data_uri(uri):
    """The provisioning URI as an SVG image the page can show with <img src="data:...">, which the CSP already allows."""
    buf = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return 'data:image/svg+xml;base64,' + base64.b64encode(buf.getvalue()).decode()


def verify(secret, code, last_step=0, now=None):
    """The 30 s step the code belongs to, or None. Allows one step of clock drift either way, and refuses any step at
    or before `last_step`, so a code that already signed someone in cannot be used again."""
    code = ''.join(ch for ch in str(code) if ch.isdigit())  # people type "123 456"
    if len(code) != 6:
        return None
    now = time.time() if now is None else now
    totp = pyotp.TOTP(secret, interval=STEP)
    for offset in (0, -1, 1):
        step = int(now // STEP) + offset
        if step > (last_step or 0) and hmac.compare_digest(totp.at(step * STEP), code):
            return step
    return None
