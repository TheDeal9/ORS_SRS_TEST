"""Shared fixtures. Tests run on SQLite by default; set TEST_DATABASE_URL to run the same suite on Postgres."""
import os
import re

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
from app import create_app, db

PASSWORD = 'correct horse battery staple'
PASSWORD_HASH = generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000')  # fast hash keeps the suite quick; one test covers the scrypt default


def make_app(tmp_path, **overrides):
    config = dict(
        TESTING=True,
        SECRET_KEY='test-secret-key-not-for-production',
        ADMIN_USERNAME='admin',
        ADMIN_PASSWORD_HASH=PASSWORD_HASH,
        SQLALCHEMY_DATABASE_URI=os.environ.get('TEST_DATABASE_URL') or f'sqlite:///{tmp_path / "test.db"}',
        WTF_CSRF_ENABLED=False,
        RATELIMIT_ENABLED=False,
        SESSION_COOKIE_SECURE=False,
        TIMEZONE='UTC',
    )
    config.update(overrides)
    app = create_app(config)
    with app.app_context():
        db.drop_all()  # clean slate when pointing at a shared Postgres
        db.create_all()
    return app


@pytest.fixture
def app(tmp_path):
    app = make_app(tmp_path)
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def anon(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    c = app.test_client()
    r = c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD})
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin')
    return c


# ---- helpers used by several test files -------------------------------------------------

def add_client(admin, first='Jane', last='Patel', band='adult'):
    r = admin.post('/admin/clients', data={'first_name': first, 'last_name': last, 'age_band': band})
    assert r.status_code == 302, r.data
    return r.headers['Location'].rsplit('/', 1)[-1]  # the new client_id


def new_link(admin, client_id, kinds='ORS,SRS'):
    """Create a session link and return its token, read from the one-time display."""
    r = admin.post(f'/admin/client/{client_id}/links', data={'kinds': kinds}, follow_redirects=True)
    assert r.status_code == 200
    m = re.search(r'value="http://localhost/client/([^"]+)"', r.get_data(as_text=True))
    assert m, 'one-time link not shown'
    return m.group(1)


def submit(anon, token, kind, positions):
    return anon.post('/api/save', json={'token': token, 'type': kind, 'positions': positions})


ORS_MARKS = [0.2, 0.5, 0.8, 1.0]      # scores 2, 5, 8, 10 -> total 25
SRS_MARKS = [0.9, 0.95, 0.99, 1.0]    # scores 9, 9.5, 9.9, 10 -> total 38.4


def complete_session(admin, anon, client_id, ors=ORS_MARKS, srs=SRS_MARKS):
    token = new_link(admin, client_id)
    assert submit(anon, token, 'ORS', ors).status_code == 200
    assert submit(anon, token, 'SRS', srs).status_code == 200
    return token


def db_count(app, model):
    with app.app_context():
        return model.query.count()


__all__ = ['app_module']
