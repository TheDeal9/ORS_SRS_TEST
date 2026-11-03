"""ORS/SRS private assessment portal.

Clients open a one-time session link and mark the scales; scores are computed on
the server and are only ever shown to the signed-in administrator who owns the client.
Run `python app.py setup` once to store the login in a git-ignored .env; `hash-password` makes the hash for a host.
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
import operator
from functools import reduce, wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import pyexcel
from flask import (Blueprint, Flask, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from openpyxl import load_workbook
from sqlalchemy import Numeric, and_, case, cast, event, exists, func, inspect, not_, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, selectinload
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import mailer
import scoring
import twofactor
from charts import domains_svg, outcome_bar_svg, session_means_svg, trend_svg

VERSION = '0.1.1'
BASE = Path(__file__).resolve().parent
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
PER_PAGE = 20
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

db = SQLAlchemy()
csrf = CSRFProtect()
# ponytail: in-memory counters are per process; fine for one gunicorn worker, use a shared store if scaled out
limiter = Limiter(get_remote_address, storage_uri='memory://')
bp = Blueprint('main', __name__)


@event.listens_for(Engine, 'connect')
def _sqlite_foreign_keys(dbapi_conn, _record):
    if isinstance(dbapi_conn, sqlite3.Connection):  # SQLite ignores FKs unless asked
        dbapi_conn.execute('PRAGMA foreign_keys=ON')


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC everywhere


# ---------------------------------------------------------------- models

class Client(db.Model):
    # The Client ID is unique per administrator, so one practitioner's "jane-patel" never shows that another has one too.
    __table_args__ = (db.UniqueConstraint('admin_id', 'slug'),)
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=False, index=True)  # the owner: no other admin sees this row
    slug = db.Column(db.String(200), nullable=False)  # the Client ID, e.g. jane-patel-2; every lookup uses it with admin_id
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    age_band = db.Column(db.String(10), nullable=False, default='adult')
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    # Summary, rewritten by refresh_summary() on every assessment change, so the client list can
    # filter, sort and page in SQL without loading anyone's assessments.
    session_count = db.Column(db.Integer, nullable=False, default=0)
    last_session_on = db.Column(db.Date, index=True)
    ors_latest = db.Column(db.Float)
    srs_latest = db.Column(db.Float)
    flag_codes = db.Column(db.String(120), nullable=False, default='')  # ',ors_low,srs_low,' so LIKE '%,code,%' matches whole codes
    needs_attention = db.Column(db.Boolean, nullable=False, default=False, index=True)
    admin = db.relationship('Admin', back_populates='clients')
    links = db.relationship('Link', back_populates='client', cascade='all, delete-orphan')
    assessments = db.relationship('Assessment', back_populates='client', cascade='all, delete-orphan',
                                  order_by='Assessment.session_no, Assessment.type')

    @property
    def flag_list(self):
        return [scoring.flag_chip(c, self.age_band) for c in self.flag_codes.strip(',').split(',') if c]


class Link(db.Model):
    """One-time session link. Only the SHA-256 of the token is stored."""
    id = db.Column(db.Integer, primary_key=True)
    client_pk = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    kinds = db.Column(db.String(10), nullable=False)  # 'ORS', 'SRS' or 'ORS,SRS'
    session_no = db.Column(db.Integer)  # assigned at first submission
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    client = db.relationship('Client', back_populates='links')
    assessments = db.relationship('Assessment', back_populates='link')

    def status(self):
        if self.completed_at:
            return 'completed'
        if self.revoked_at:
            return 'revoked'
        if self.expires_at <= utcnow():
            return 'expired'
        return 'active'


class Assessment(db.Model):
    # One ORS and one SRS per session number, and one of each per link, however the requests interleave (NULL links, as
    # in hand-entered rows, never clash).
    __table_args__ = (db.UniqueConstraint('client_pk', 'session_no', 'type'), db.UniqueConstraint('link_pk', 'type'))
    id = db.Column(db.Integer, primary_key=True)
    client_pk = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False, index=True)
    link_pk = db.Column(db.Integer, db.ForeignKey('link.id'), index=True)
    session_no = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(3), nullable=False)  # 'ORS' or 'SRS'
    taken_on = db.Column(db.Date, nullable=False)
    scores = db.Column(db.JSON, nullable=False)  # {canonical item name: 0-10}
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    client = db.relationship('Client', back_populates='assessments')
    link = db.relationship('Link', back_populates='assessments')

    @property
    def total(self):
        return scoring.total(self.scores)

    @property
    def ordered_scores(self):
        return [(item, self.scores[item]) for item in scoring.ITEMS[self.type]]


class Admin(db.Model):
    """A portal administrator. The password, recovery code and one-time email code are stored only as hashes; the
    authenticator-app secret only encrypted."""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    recovery_hash = db.Column(db.String(64))  # SHA-256 of the recovery code; None until one is created
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    # Sign-up state: unverified (email not proven) -> pending (owner has not approved) -> active <-> disabled.
    # Only 'active' holds a session or an API key. Accounts made by setup or the command line start 'active'.
    status = db.Column(db.String(12), nullable=False, default='active')
    is_owner = db.Column(db.Boolean, nullable=False, default=False)  # the first administrator: approves the others
    email = db.Column(db.String(254), unique=True)  # set only once the address has been proven with a code
    pending_email = db.Column(db.String(254))  # an address waiting for its code (sign-up, or a change of email)
    # One live email code per administrator (a new one replaces it). `otp_hash` is None during an authenticator-app
    # sign-in challenge, which uses the same expiry and try counter.
    otp_hash = db.Column(db.String(64))
    otp_purpose = db.Column(db.String(8))  # 'verify' or 'login'
    otp_expires_at = db.Column(db.DateTime)
    otp_tries = db.Column(db.Integer, nullable=False, default=0)
    mfa_fails = db.Column(db.Integer, nullable=False, default=0)  # wrong second-step codes since the last good sign-in
    locked_until = db.Column(db.DateTime)
    totp_secret = db.Column(db.String(200))  # Fernet-sealed; enabled only once totp_enabled_at is set
    totp_enabled_at = db.Column(db.DateTime)
    totp_last_step = db.Column(db.Integer, nullable=False, default=0)  # newest 30 s step already used: no replay
    clients = db.relationship('Client', back_populates='admin', order_by='Client.id')  # no cascade: deleting an admin must not delete data
    api_keys = db.relationship('ApiKey', back_populates='admin', cascade='all, delete-orphan', order_by='ApiKey.id')

    @property
    def stamp(self):
        """Changes whenever the password does, so a password change signs every other browser out. Cookies carry
        it too, which means SECRET_KEY alone cannot forge a session: the forger would also need this database row."""
        return hash_token(self.password_hash)[:16]


class ApiKey(db.Model):
    """Per-administrator key for the command line and scripts. Only the SHA-256 is stored; the key is shown once."""
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=False, index=True)
    label = db.Column(db.String(60), nullable=False)
    prefix = db.Column(db.String(12), nullable=False)  # first characters of the key, to tell keys apart in a list
    key_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)
    admin = db.relationship('Admin', back_populates='api_keys')


# ---------------------------------------------------------------- helpers

def make_slug(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-') or 'client'


def hash_token(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def today_local():
    return datetime.now(ZoneInfo(current_app.config['TIMEZONE'])).date()


def find_active_link(token):
    link = Link.query.filter_by(token_hash=hash_token(token)).first()
    return link if link and link.status() == 'active' else None


def submitted_kinds(link):
    """Types already saved on this link, read from the database now (not from a collection loaded earlier)."""
    return set(db.session.scalars(select(Assessment.type).where(Assessment.link_pk == link.id)))


def latest_link(client):
    return max(client.links, key=lambda link: link.id, default=None)


# ponytail: SQLite has no row locks and runs the portal in one process (dev, tests), so there one lock per row in this
# process stands in for SELECT .. FOR UPDATE. It never cleans up (one small lock per row seen); the production database
# is Postgres, where the row lock works across processes and servers.
_sqlite_row_locks = {}
_sqlite_guard = threading.Lock()


def lock_row(model, pk):
    """Hold one row until the request ends, so read-then-write steps on it cannot interleave. Postgres: SELECT ..
    FOR UPDATE. SQLite: the in-process lock above, taken before any write so two requests cannot deadlock on it.
    Returns the (current) row. The UNIQUE constraints remain the final guard."""
    if db.engine.dialect.name == 'sqlite':
        held = g.setdefault('row_locks', {})
        if (model, pk) not in held:
            with _sqlite_guard:
                lock = _sqlite_row_locks.setdefault((model.__name__, pk), threading.Lock())
            lock.acquire()
            held[(model, pk)] = lock
    return db.session.execute(select(model).where(model.id == pk).with_for_update()).scalar_one()


def lock_client(client_pk):
    """Saving and new links read a client's assessments or links and then write; taking this first means the session
    number, the summary columns and the "one live link" rule never miss a sibling's change."""
    return lock_row(Client, client_pk)


def lock_admin(admin):
    """Code checks count tries and spend codes; under this lock a burst of simultaneous guesses is counted one by one,
    and a code or authenticator step can be spent only once. Reloads the admin, which may have changed while waiting."""
    lock_row(Admin, admin.id)
    db.session.refresh(admin)
    return admin


@bp.teardown_app_request
def release_row_locks(_exc):
    for lock in g.pop('row_locks', {}).values():
        lock.release()


def next_session_no(client_pk, kind, today):
    """Next session number; an ORS and an SRS taken on the same day share one session."""
    last = Assessment.query.filter_by(client_pk=client_pk).order_by(Assessment.session_no.desc()).first()
    if last is None:
        return 1
    missing = Assessment.query.filter_by(client_pk=client_pk, session_no=last.session_no, type=kind).first() is None
    return last.session_no if last.taken_on == today and missing else last.session_no + 1


def sessions_of(assessments):
    """Group assessments into sessions: [{'no','date','ORS','SRS','ors_total','srs_total'}]."""
    by = {}
    for a in assessments:
        s = by.setdefault(a.session_no, {'no': a.session_no, 'date': a.taken_on, 'ORS': None, 'SRS': None})
        s[a.type] = a
        s['date'] = min(s['date'], a.taken_on)
    out = []
    for no in sorted(by):
        s = by[no]
        s['ors_total'] = s['ORS'].total if s['ORS'] else None
        s['srs_total'] = s['SRS'].total if s['SRS'] else None
        out.append(s)
    return out


def flags_for(client, sessions):
    ors = [s['ors_total'] for s in sessions if s['ORS']]
    srs = next((s['SRS'].scores for s in reversed(sessions) if s['SRS']), None)
    return scoring.client_flags(client.age_band, ors, srs)


def refresh_summary(client):
    """Rewrite the client's summary columns from its assessments. Call before every commit that adds
    or removes an assessment; `rebuild-summaries` repairs drift."""
    rows = Assessment.query.filter_by(client_pk=client.id).order_by(Assessment.session_no, Assessment.type).all()
    sessions = sessions_of(rows)
    flags = flags_for(client, sessions)
    client.session_count = len(sessions)
    client.last_session_on = sessions[-1]['date'] if sessions else None
    client.ors_latest = next((s['ors_total'] for s in reversed(sessions) if s['ORS']), None)
    client.srs_latest = next((s['srs_total'] for s in reversed(sessions) if s['SRS']), None)
    client.flag_codes = ',' + ','.join(f['code'] for f in flags) + ',' if flags else ''
    client.needs_attention = any(f['level'] == 'warn' for f in flags)


STATUS_LABELS = {'none': 'No link', 'waiting': 'Awaiting client', 'in_progress': 'In progress',
                 'completed': 'Completed', 'expired': 'Expired', 'revoked': 'Revoked'}


def link_state(link):
    """The client list's Status column. Mirrors state_expr(), which does the same inside SQL for filtering."""
    if link is None:
        return 'none'
    status = link.status()
    if status != 'active':
        return status
    return 'in_progress' if link.assessments else 'waiting'


def state_expr(latest):
    return case((latest.id.is_(None), 'none'),
                (latest.completed_at.isnot(None), 'completed'),
                (latest.revoked_at.isnot(None), 'revoked'),
                (latest.expires_at <= utcnow(), 'expired'),
                (exists().where(Assessment.link_pk == latest.id), 'in_progress'),
                else_='waiting')


def link_message(link):
    if link is None:
        return 'No session link has been created yet.'
    status = link.status()
    if status == 'completed':
        return f'Session {link.session_no} complete. The link has been used.'
    if status in ('revoked', 'expired'):
        return f'The last link was {status}.'
    got = submitted_kinds(link)
    waiting = ' and '.join(k for k in link.kinds.split(',') if k not in got)
    if not got:
        return f'Waiting for the client to complete {waiting}.'
    return f'{" and ".join(sorted(got))} received. Waiting for {waiting}.'


def activity_state(client):
    """Changes whenever the client page should refresh (new result or link state change)."""
    link = latest_link(client)
    return f'{len(client.assessments)}|{link.id}|{link.status()}' if link else f'{len(client.assessments)}|0|none'


SORTS = {
    'new': ('Newest first', lambda: [Client.created_at.desc(), Client.id.desc()]),
    'name': ('Name A to Z', lambda: [func.lower(Client.first_name), func.lower(Client.last_name), Client.id]),
    'recent': ('Last session, newest', lambda: [Client.last_session_on.desc().nulls_last(), Client.id.desc()]),
    'ors': ('Lowest ORS first', lambda: [Client.ors_latest.asc().nulls_last(), Client.id]),
    'attention': ('Needs attention first', lambda: [Client.needs_attention.desc(), Client.created_at.desc(), Client.id.desc()]),
}


def like(text):
    return '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def read_filters(args):
    """Whitelist every filter value; anything unknown is ignored rather than trusted."""
    def pick(name, allowed):
        return args.get(name) if args.get(name) in allowed else ''
    return {'q': args.get('q', '').strip()[:80], 'attention': pick('attention', ('1', '0')),
            'status': pick('status', STATUS_LABELS), 'band': pick('band', scoring.ORS_CUTOFF),
            'flag': pick('flag', scoring.FLAG_INFO), 'sort': pick('sort', SORTS) or 'new'}


def client_select(f):
    """SELECT of the signed-in administrator's clients matching the filters. Status comes from each client's newest link."""
    newest = select(Link.client_pk, func.max(Link.id).label('lid')).group_by(Link.client_pk).subquery()
    latest = aliased(Link)
    q = (select(Client).where(Client.admin_id == g.admin.id)
         .outerjoin(newest, newest.c.client_pk == Client.id).outerjoin(latest, latest.id == newest.c.lid))
    if f['q']:
        q = q.where(or_(Client.slug.ilike(like(f['q']), escape='\\'),
                        (Client.first_name + ' ' + Client.last_name).ilike(like(f['q']), escape='\\')))
    if f['attention']:
        q = q.where(Client.needs_attention.is_(f['attention'] == '1'))
    if f['band']:
        q = q.where(Client.age_band == f['band'])
    if f['flag']:
        q = q.where(Client.flag_codes.like(f'%,{f["flag"]},%'))  # safe: flag is a whitelisted code
    if f['status']:
        q = q.where(state_expr(latest) == f['status'])
    return q


def count_clients(**overrides):
    f = {**read_filters({}), **overrides}
    return db.session.scalar(select(func.count()).select_from(client_select(f).subquery()))


# ---------------------------------------------------------------- calculations done in the database
# Totals, changes and caseload figures are worked out in SQL (window functions and GROUP BY), so a long history or a big
# caseload costs one query, not a loop over rows. Every threshold comes from scoring.py, never retyped here.
# Python keeps the per-client flag rules (they need the whole history in order) and finishes the last arithmetic step
# of the effect size; tests check that both sides agree.

def _two_places(expr):
    """expr rounded to 2 decimals as a plain float on both SQLite and Postgres (Numeric first: Postgres has no
    round(double precision, int))."""
    return func.round(cast(expr, Numeric(12, 4, asdecimal=False)), 2)


def total_sql(kind=None):
    """The four item scores added up, as scoring.total does. Without `kind`, ORS rows use the ORS items and SRS rows the SRS ones."""
    def of(k):
        return reduce(operator.add, (Assessment.scores[item].as_float() for item in scoring.ITEMS[k]))
    return _two_places(of(kind) if kind else case((Assessment.type == 'ORS', of('ORS')), else_=of('SRS')))


def client_series(client_pk):
    """Every assessment of one client in session order with its total and, in the same query, the change since that
    type's first result and since its previous one (window functions). The client must already be checked as the
    signed-in administrator's. Returns dicts: id, no, type, date, total, since_intake, since_prev (None where none)."""
    total = total_sql()
    w = {'partition_by': Assessment.type, 'order_by': Assessment.session_no}
    rows = db.session.execute(
        select(Assessment.id, Assessment.session_no, Assessment.type, Assessment.taken_on, total.label('total'),
               _two_places(total - func.first_value(total).over(**w)).label('since_intake'),
               _two_places(total - func.lag(total).over(**w)).label('since_prev'))
        .where(Assessment.client_pk == client_pk).order_by(Assessment.session_no, Assessment.type))
    return [{'id': r.id, 'no': r.session_no, 'type': r.type, 'date': r.taken_on, 'total': float(r.total),
             'since_intake': None if r.since_prev is None else float(r.since_intake),  # the first result has no change to report
             'since_prev': None if r.since_prev is None else float(r.since_prev)} for r in rows]


def caseload_stats(admin_id):
    """One administrator's whole caseload from their clients' first and latest ORS: counts by outcome and the sums the
    effect size needs. Outcome classes follow scoring.ors_status exactly: reliable improvement (up), the same plus
    crossing the cutoff (csc), reliable decline (down), otherwise no reliable change. Outcomes are judged for clients
    with two or more ORS; 'above' counts every client with at least one."""
    t = total_sql('ORS')
    ranked = (select(Assessment.client_pk.label('cpk'), t.label('t'),
                     func.row_number().over(partition_by=Assessment.client_pk, order_by=Assessment.session_no.asc()).label('r_first'),
                     func.row_number().over(partition_by=Assessment.client_pk, order_by=Assessment.session_no.desc()).label('r_last'),
                     func.count().over(partition_by=Assessment.client_pk).label('n'))
              .join(Client, Client.id == Assessment.client_pk).where(Client.admin_id == admin_id, Assessment.type == 'ORS').subquery())
    first = select(ranked.c.cpk, ranked.c.t.label('first_t'), ranked.c.n).where(ranked.c.r_first == 1).subquery()
    last = select(ranked.c.cpk, ranked.c.t.label('last_t')).where(ranked.c.r_last == 1).subquery()
    cutoff = case(*[(Client.age_band == band, c) for band, c in scoring.ORS_CUTOFF.items()], else_=scoring.ORS_CUTOFF['adult'])
    per = (select(cutoff.label('cut'), first.c.first_t, last.c.last_t, first.c.n, _two_places(last.c.last_t - first.c.first_t).label('delta'))
           .select_from(Client).join(first, first.c.cpk == Client.id).join(last, last.c.cpk == Client.id)
           .where(Client.admin_id == admin_id).subquery())
    rc = scoring.ORS_RELIABLE_CHANGE
    two = per.c.n >= 2
    csc = and_(two, per.c.delta >= rc, per.c.first_t < per.c.cut, per.c.last_t >= per.c.cut)
    up = and_(two, per.c.delta >= rc, not_(csc))
    down = and_(two, per.c.delta <= -rc)
    flat = and_(two, per.c.delta > -rc, per.c.delta < rc)

    def count(cond):
        return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)

    def total_of(cond, value):
        return func.coalesce(func.sum(case((cond, value), else_=0)), 0)
    r = db.session.execute(select(
        func.count().label('with_ors'), count(per.c.last_t >= per.c.cut).label('above'), count(two).label('judged'),
        count(csc).label('csc'), count(up).label('up'), count(flat).label('flat'), count(down).label('down'),
        total_of(two, per.c.delta).label('sum_change'), total_of(two, per.c.first_t).label('sum_intake'),
        total_of(two, per.c.first_t * per.c.first_t).label('sum_intake_sq'))).one()
    out = {k: (float(v) if k.startswith('sum_') else int(v)) for k, v in r._mapping.items()}
    out['effect_size'] = scoring.effect_size(out['judged'], out['sum_change'], out['sum_intake'], out['sum_intake_sq'])
    out['mean_change'] = round(out['sum_change'] / out['judged'], 1) if out['judged'] else None
    return out


def caseload_by_session(admin_id):
    """Mean total and number of clients at each session number, ORS and SRS separately: [{'no','ORS':(mean,n)|None,'SRS':...}]."""
    rows = db.session.execute(
        select(Assessment.type, Assessment.session_no, func.avg(total_sql()).label('mean'), func.count().label('n'))
        .join(Client, Client.id == Assessment.client_pk).where(Client.admin_id == admin_id)
        .group_by(Assessment.type, Assessment.session_no).order_by(Assessment.session_no))
    by = {}
    for r in rows:
        by.setdefault(r.session_no, {'no': r.session_no, 'ORS': None, 'SRS': None})[r.type] = (round(float(r.mean), 1), int(r.n))
    return [by[k] for k in sorted(by)]



def report_tables(client):
    """The PCOMS workbook as {'ORS': (header, rows), 'SRS': ..., 'Total': ...}, read back through
    pyexcel so the portal shows what the download contains."""
    n = len(sessions_of(client.assessments))
    book = pyexcel.get_book(file_type='xlsx', file_content=build_workbook(client).getvalue())
    out = {}
    for name in ('ORS', 'SRS'):
        rows = [(r + [None] * 5)[:5] for r in book[name].to_array()]
        out[name] = (rows[0], rows[1:1 + n])
    # Excel computes the Total sheet from formulas, and a file written by code has no cached results,
    # so work out the same sums here (date, ORS sum, SRS sum) from the sheets just read.
    def tot(row):
        nums = [v for v in row[1:5] if isinstance(v, (int, float))]
        return round(sum(nums), 2) if nums else None
    out['Total'] = (['Date', 'ORS Total', 'SRS Total'],
                    [[o[0] or s[0], tot(o), tot(s)] for o, s in zip(out['ORS'][1], out['SRS'][1])])
    return out


def build_workbook(client):
    """Fill the PCOMS template from the database. One row per session. Returns BytesIO."""
    wb = load_workbook(current_app.config['TEMPLATE_FILE'])
    for name in ('ORS', 'SRS'):
        ws = wb[name]
        for row in range(2, ws.max_row + 1):
            for col in range(1, 6):
                ws.cell(row, col).value = None
    total = wb['Total']
    for row in range(2, total.max_row + 1):
        for col in range(1, 4):
            total.cell(row, col).value = None
    for row, s in enumerate(sessions_of(client.assessments), start=2):
        for kind in ('ORS', 'SRS'):
            if s[kind]:
                ws = wb[kind]
                ws.cell(row, 1).value = s[kind].taken_on.isoformat()
                for col, (_item, value) in enumerate(s[kind].ordered_scores, start=2):
                    ws.cell(row, col).value = value
        total.cell(row, 1).value = f'=IF(ORS!A{row}<>"",ORS!A{row},SRS!A{row})'
        total.cell(row, 2).value = f'=IF(COUNT(ORS!B{row}:E{row})=0,"",SUM(ORS!B{row}:E{row}))'
        total.cell(row, 3).value = f'=IF(COUNT(SRS!B{row}:E{row})=0,"",SUM(SRS!B{row}:E{row}))'
    wb.properties.title = f'PCOMS report - {client.slug}'
    wb.properties.subject = f'Client ID: {client.slug}'
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------- administrators: accounts, sessions, keys

MIN_PASSWORD_LEN = 10
USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{1,63}$')
# scrypt:N:r:p$salt$hex or pbkdf2:sha256[:iterations]$salt$hex. A shell that expands the `$` signs leaves something that fails this.
HASH_RE = re.compile(r'^(scrypt:\d+:\d+:\d+|pbkdf2:[a-z0-9]+(:\d+)?)\$[^$\s]+\$[0-9a-f]+$')
# Checked when the ID is unknown, so a wrong ID and a wrong password take equally long.
DUMMY_HASH = generate_password_hash(secrets.token_hex(8))
# A deliberately plain shape check; the code mailed to the address is the real proof. No spaces, quotes or commas keeps
# the address safe to put in a mail header.
EMAIL_RE = re.compile(r'^[^@\s<>,;"\\]{1,64}@[^@\s<>,;"\\]+\.[^@\s<>,;"\\]{2,}$')
OTP_TTL = timedelta(minutes=10)
OTP_COOLDOWN = timedelta(seconds=60)  # the earliest a new code may be sent after the last one
OTP_MAX_TRIES = 5  # wrong guesses a single code survives
MFA_MAX_FAILS = 10  # wrong second-step codes in a row before the account's second step locks
MFA_LOCK = timedelta(minutes=15)
PRE_TTL = 600  # seconds a half-signed-in browser (password right, code not yet) may take to finish


def norm_username(value):
    return str(value).strip().lower()


def password_problem(password, confirm=None):
    if len(password) < MIN_PASSWORD_LEN:
        return f'Use at least {MIN_PASSWORD_LEN} characters.'
    if len(password) > 128:
        return 'Use 128 characters or fewer.'
    if confirm is not None and password != confirm:
        return 'The two passwords differ.'
    return ''


def new_recovery_code():
    raw = secrets.token_hex(12)  # 96 bits, so the 5-per-minute limit makes guessing hopeless
    return '-'.join(raw[i:i + 4] for i in range(0, 24, 4))


def recovery_hash(code):
    return hash_token(re.sub(r'[^0-9a-f]', '', str(code).lower()))  # spaces, dashes and case do not matter


def issue_recovery_code(admin):
    """Replace the admin's recovery code. Returns the new code, which is never stored in readable form."""
    code = new_recovery_code()
    admin.recovery_hash = recovery_hash(code)
    return code


def revoke_api_keys(admin):
    now = utcnow()
    for key in admin.api_keys:
        key.revoked_at = key.revoked_at or now


def new_api_key(admin, label):
    key = 'ors_' + secrets.token_urlsafe(32)
    db.session.add(ApiKey(admin_id=admin.id, label=label, prefix=key[:12], key_hash=hash_token(key)))
    return key


def norm_email(value):
    return str(value or '').strip().lower()


def email_problem(email):
    return '' if len(email) <= 254 and EMAIL_RE.match(email) else 'Enter a valid email address.'


def create_admin(username, password, email=None, status='active'):
    """Add an administrator and return (admin, recovery_code). Raises ValueError with a message fit to show.
    The first administrator ever created is the owner. An email given here waits as `pending_email` until its owner
    proves it with a code, so a typo can never lock anyone out. A sign-up (status 'unverified') gets its recovery
    code later, when the address is proven, so `code` is None for it."""
    username = norm_username(username)
    if not USERNAME_RE.match(username):
        raise ValueError('The ID must be 2 to 64 characters: lower-case letters, digits, dot, dash or underscore.')
    if password_problem(password):
        raise ValueError(password_problem(password))
    email = norm_email(email) or None
    if email and email_problem(email):
        raise ValueError(email_problem(email))
    if Admin.query.filter_by(username=username).first():
        raise ValueError(f'The ID "{username}" already exists.')
    if email and Admin.query.filter_by(email=email).first():
        raise ValueError('That email address already has an account.')
    admin = Admin(username=username, password_hash=generate_password_hash(password), status=status,
                  pending_email=email, is_owner=Admin.query.first() is None)
    code = issue_recovery_code(admin) if status == 'active' else None
    db.session.add(admin)
    db.session.commit()
    return admin, code


def reset_password(admin, password):
    """Forgotten-password reset (recovery code or command line): new password, every browser signed out,
    API keys revoked, fresh recovery code returned. Treats the old credentials as possibly exposed."""
    if password_problem(password):
        raise ValueError(password_problem(password))
    admin.password_hash = generate_password_hash(password)
    revoke_api_keys(admin)
    clear_otp(admin)
    code = issue_recovery_code(admin)
    db.session.commit()
    return code


def authenticate(username, password):
    admin = Admin.query.filter_by(username=norm_username(username)).first()
    ok = check_password_hash(admin.password_hash if admin else DUMMY_HASH, password)
    return admin if admin and ok else None


def seed_admin(require=True):
    """First start only: create the first administrator from ORS_SRS_ADMIN_USERNAME and ORS_SRS_ADMIN_PASSWORD_HASH
    while the admin table is empty. After that the database is the source of truth and those variables are ignored."""
    if Admin.query.first():
        return
    cfg = current_app.config
    given = cfg['ADMIN_PASSWORD_HASH'] or ''
    if HASH_RE.match(given):
        username = norm_username(cfg['ADMIN_USERNAME'])
        if not USERNAME_RE.match(username):
            raise RuntimeError('ORS_SRS_ADMIN_USERNAME must be 2 to 64 characters: letters, digits, dot, dash or underscore.')
        db.session.add(Admin(username=username, password_hash=given, is_owner=True))
        try:
            db.session.commit()
        except IntegrityError:  # another worker seeded at the same moment
            db.session.rollback()
    elif require:
        raise RuntimeError('No administrator exists yet, and ORS_SRS_ADMIN_PASSWORD_HASH is missing or not a valid password hash '
                           '(a shell that expands the $ signs corrupts it: use single quotes). '
                           'Run `python app.py setup`, or `python app.py hash-password` and paste the result into the host settings.')


def current_admin():
    """The signed-in administrator, or None. The cookie must carry this admin's current stamp, and the account must
    still be active, so disabling an administrator ends their sessions at once. A browser that has only passed the
    password step holds `pre`, not `aid`, and is therefore not signed in."""
    admin = db.session.get(Admin, session['aid']) if session.get('aid') else None
    return admin if admin and admin.status == 'active' and secrets.compare_digest(str(session.get('stamp', '')), admin.stamp) else None


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        g.admin = current_admin()
        if not g.admin:
            session.clear()
            return redirect(url_for('main.admin_login'))
        return view(*args, **kwargs)
    return wrapper


def owner_required(view):
    """Owner-only pages. Everyone else gets 404, the same as a page that does not exist."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.admin.is_owner:
            abort(404)
        return view(*args, **kwargs)
    return admin_required(wrapper)


# ---------------------------------------------------------------- second step: email codes and the half-signed-in state

def mail_ready():
    return bool(current_app.config['MAIL_BACKEND'])


def signup_open():
    return mail_ready() and current_app.config['SIGNUP_ENABLED']


def send_mail(to, subject, body):
    """Send one message. Failures are logged by cause only (never a body, so never a code) and raised as MailError."""
    try:
        mailer.send(current_app.config, to, subject, body)
    except mailer.MailError as exc:
        current_app.logger.error('mail not sent (%s)', exc)
        raise


def mask_email(email):
    name, _, domain = (email or '').partition('@')
    return f'{name[:1]}***@{domain}' if domain else ''


def otp_digest(admin, purpose, code):
    """Keyed hash of a code. Tied to the admin and the purpose, and to SECRET_KEY, so a copy of the database alone cannot
    be checked against a table of all million codes."""
    return hmac.new(current_app.config['SECRET_KEY'].encode(), f'{admin.id}:{purpose}:{code}'.encode(), 'sha256').hexdigest()


def clear_otp(admin):
    admin.otp_hash = admin.otp_purpose = admin.otp_expires_at = None
    admin.otp_tries = 0


def challenge_live(admin, purpose):
    return bool(admin.otp_purpose == purpose and admin.otp_expires_at and utcnow() < admin.otp_expires_at
                and admin.otp_tries < OTP_MAX_TRIES)


def send_otp(admin, purpose, to):
    """Email a fresh six-digit code, replacing any earlier one. Returns 'sent', 'wait' (a code went out less than a
    minute ago, so the earlier one stays valid) or 'failed' (the mail could not be sent; nothing stays pending)."""
    if admin.otp_hash and admin.otp_expires_at and utcnow() < admin.otp_expires_at - OTP_TTL + OTP_COOLDOWN:
        return 'wait'
    code = f'{secrets.randbelow(10 ** 6):06d}'
    admin.otp_hash, admin.otp_purpose = otp_digest(admin, purpose, code), purpose
    admin.otp_expires_at, admin.otp_tries = utcnow() + OTP_TTL, 0
    what = 'confirm this email address' if purpose == 'verify' else 'sign in'
    try:
        send_mail(to, f'Your ORS/SRS portal code: {code}',
                  f'Your one-time code to {what} is {code}.\n\nIt works once and expires in 10 minutes. '
                  f'If you did not ask for it, ignore this message: nothing has changed on your account.')
    except mailer.MailError:
        clear_otp(admin)
        db.session.commit()
        return 'failed'
    db.session.commit()
    return 'sent'


def check_otp(admin, purpose, code):
    """True if `code` is this admin's live code for `purpose`; the code is then spent. A wrong guess counts against the
    code, which dies after OTP_MAX_TRIES. The caller commits."""
    live = challenge_live(admin, purpose) and bool(admin.otp_hash)
    digits = ''.join(ch for ch in str(code) if ch.isdigit())
    match = secrets.compare_digest(otp_digest(admin, purpose, digits), admin.otp_hash or '0' * 64)
    if live and match:
        clear_otp(admin)
        return True
    if live:
        admin.otp_tries += 1
    return False


def second_step_locked(admin):
    return bool(admin.locked_until and utcnow() < admin.locked_until)


def note_second_step_failure(admin):
    admin.mfa_fails += 1
    if admin.mfa_fails >= MFA_MAX_FAILS:
        admin.mfa_fails, admin.locked_until = 0, utcnow() + MFA_LOCK


def second_step_method(admin):
    """'totp' (authenticator app), 'email', or '' when this admin has no second step set up. A sealed secret that can no
    longer be read (SECRET_KEY changed) falls back to email. An admin with an email but no working mail is refused, not
    waved through: a missing second step must never be a way in."""
    if admin.totp_enabled_at and twofactor.unseal(admin.totp_secret, current_app.config['SECRET_KEY']):
        return 'totp'
    return 'email' if admin.email else ''


def set_pre(admin, purpose, method='email'):
    """Remember that this browser passed the password but not the code. Holds no `aid`, so nothing else opens."""
    session.clear()
    session['pre'] = {'aid': admin.id, 'stamp': admin.stamp, 'purpose': purpose, 'method': method,
                      'exp': int(time.time()) + PRE_TTL}


def pre_admin(purpose):
    """The administrator half-way through `purpose` ('verify' or 'login') in this browser, or None."""
    pre = session.get('pre')
    if not isinstance(pre, dict) or pre.get('purpose') != purpose or pre.get('exp', 0) < time.time():
        return None
    admin = db.session.get(Admin, pre.get('aid') or 0)
    if not admin or not secrets.compare_digest(str(pre.get('stamp', '')), admin.stamp):
        return None
    if (purpose == 'login' and admin.status != 'active') or (purpose == 'verify' and admin.status not in ('unverified', 'active')):
        return None
    return admin


def finish_login(admin):
    """The one place a browser becomes signed in."""
    session.clear()
    session.update(aid=admin.id, stamp=admin.stamp)
    session.permanent = True
    admin.last_login_at = utcnow()
    admin.mfa_fails, admin.locked_until = 0, None
    clear_otp(admin)
    db.session.commit()
    if not admin.recovery_hash:
        flash('You have no recovery code yet. Create one under Account so you can reset a forgotten password.')
    elif not admin.email and mail_ready():
        flash('Add an email address under Account to turn on two-step sign-in.')
    return redirect(url_for('main.dashboard'))


def api_required(view):
    """Bearer-key authentication for /api/v1. Deliberately ignores the session cookie, so these CSRF-free
    endpoints can never be driven by a browser that happens to be signed in."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get('Authorization', '')
        token = header[7:].strip() if header[:7].lower() == 'bearer ' else ''
        key = ApiKey.query.filter_by(key_hash=hash_token(token)).first() if token else None
        if not key or key.revoked_at or key.admin.status != 'active':
            resp = jsonify(error='Missing or invalid API key. Send the header "Authorization: Bearer <key>".')
            resp.status_code = 401
            resp.headers['WWW-Authenticate'] = 'Bearer'
            return resp
        g.admin, g.api_key = key.admin, key
        now = utcnow()
        if not key.last_used_at or now - key.last_used_at > timedelta(minutes=1):  # at most one write a minute
            key.last_used_at = now
            db.session.commit()
        return view(*args, **kwargs)
    return wrapper


def get_client(slug):
    """The signed-in administrator's client, or 404. Someone else's client is indistinguishable from a missing one."""
    return Client.query.filter_by(admin_id=g.admin.id, slug=slug).first_or_404()


def get_assessment(assessment_id):
    """An assessment of the signed-in administrator's client, or 404 (ids are sequential, so they must be checked)."""
    return Assessment.query.join(Client).filter(Assessment.id == assessment_id, Client.admin_id == g.admin.id).first_or_404()


def error(message, status):
    return jsonify(error=message), status


# ---------------------------------------------------------------- public + client routes

@bp.get('/')
def index():
    return render_template('landing.html', signup=signup_open())


@bp.get('/health')
def health():
    return jsonify(status='ok')


@bp.get('/client/<token>')
def client_assessment(token):
    link = find_active_link(token)
    if not link:
        return render_template('closed.html'), 404
    got = submitted_kinds(link)
    pending = [k for k in link.kinds.split(',') if k not in got]
    return render_template('assessment.html', token=token, client=link.client, pending=pending)


@bp.post('/api/save')
@csrf.exempt  # authenticated by the one-time token in the body, not by a cookie
def save_assessment():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('Invalid request.', 400)
    link = find_active_link(data.get('token', ''))
    if not link:
        return error('This assessment link is no longer valid. Please contact your practitioner.', 403)
    kind = data.get('type')
    if kind not in link.kinds.split(','):
        return error('Unknown assessment type.', 400)
    try:
        scores = scoring.scores_from_positions(kind, data.get('positions'))
    except ValueError:
        return error('Please mark all four scales before finishing.', 400)
    link_id, client_pk = link.id, link.client_pk
    for _attempt in range(3):  # a concurrent save can claim our session number; retry with a fresh one
        lock_client(client_pk)  # saves for one client run one after another, so the summary never misses a sibling's row
        link = db.session.get(Link, link_id, populate_existing=True)  # re-read under the lock: it may have changed
        if link.status() != 'active':
            return error('This assessment link is no longer valid. Please contact your practitioner.', 403)
        already = submitted_kinds(link)
        if kind in already:
            return error('This assessment has already been submitted.', 409)
        today = today_local()
        if link.session_no is None:
            link.session_no = next_session_no(client_pk, kind, today)
        done = (already | {kind}) >= set(link.kinds.split(','))
        try:
            db.session.add(Assessment(client_pk=client_pk, link_pk=link.id, session_no=link.session_no,
                                      type=kind, taken_on=today, scores=scores))
            if done:
                link.completed_at = utcnow()
            refresh_summary(db.session.get(Client, client_pk))  # its query flushes the insert, so a clash raises here
            db.session.commit()
            return jsonify(ok=True, done=done)
        except IntegrityError:
            db.session.rollback()
    return error('Could not save right now. Please try again.', 409)


# ---------------------------------------------------------------- admin auth

@bp.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def admin_login():
    if request.method == 'POST':
        admin = authenticate(request.form.get('username', ''), request.form.get('password', ''))
        if admin:
            # Only a correct password reveals what state the account is in, so IDs cannot be probed through these.
            if admin.status == 'unverified':
                if admin.pending_email and mail_ready():
                    send_otp(admin, 'verify', admin.pending_email)
                    set_pre(admin, 'verify')
                    return redirect(url_for('main.admin_verify'))
                flash('This sign-up was never confirmed. Please sign up again.')
            elif admin.status == 'pending':
                flash('Your email is confirmed, but the portal owner has not approved your account yet. You will get an email when they do.')
            elif admin.status == 'disabled':
                flash('This account is disabled. Ask the portal owner.')
            else:
                method = second_step_method(admin)
                if not method:
                    return finish_login(admin)
                if method == 'email' and not mail_ready():
                    flash('Email codes are not available on this server right now, so sign-in is paused. Ask the portal owner.')
                else:
                    return begin_second_step(admin, method)
        else:
            flash('Incorrect administrator ID or password.')  # one message for both, so IDs cannot be probed
    return render_template('login.html', signup=signup_open())


def begin_second_step(admin, method):
    """Password was right: ask for a code. The browser is not signed in until /admin/2fa accepts it."""
    lock_admin(admin)
    if method == 'email':
        if send_otp(admin, 'login', admin.email) == 'failed':
            flash('We could not email the code. Please try again in a minute.')
            return render_template('login.html', signup=signup_open())
    else:  # authenticator app: no mail, but the same ten minutes and five tries
        clear_otp(admin)
        admin.otp_purpose, admin.otp_expires_at = 'login', utcnow() + OTP_TTL
        db.session.commit()
    set_pre(admin, 'login', method)
    return redirect(url_for('main.admin_2fa'))


@bp.route('/admin/2fa', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def admin_2fa():
    admin = pre_admin('login')
    if not admin:
        return redirect(url_for('main.admin_login'))
    method = session['pre']['method']
    if request.method == 'POST':
        lock_admin(admin)
        code = request.form.get('code', '')
        if second_step_locked(admin):
            flash('Too many wrong codes. This account\'s second step is locked for a few minutes.')
        else:
            ok = False
            if method == 'totp':
                secret = twofactor.unseal(admin.totp_secret, current_app.config['SECRET_KEY'])
                step = twofactor.verify(secret, code, admin.totp_last_step) if secret and challenge_live(admin, 'login') else None
                if step:
                    admin.totp_last_step, ok = step, True
                elif challenge_live(admin, 'login'):
                    admin.otp_tries += 1
            else:
                ok = check_otp(admin, 'login', code)
            if ok:
                return finish_login(admin)
            note_second_step_failure(admin)
            db.session.commit()
            flash('That code is not right or has expired.')
    return render_template('twofactor.html', method=method, email=mask_email(admin.email), locked=second_step_locked(admin))


@bp.post('/admin/2fa/resend')
@limiter.limit('5 per minute')
def admin_2fa_resend():
    admin = pre_admin('login')
    if not admin:
        return redirect(url_for('main.admin_login'))
    lock_admin(admin)
    if second_step_locked(admin) or not admin.email or not mail_ready():
        flash('A code cannot be sent right now.')
    else:
        result = send_otp(admin, 'login', admin.email)
        if result == 'sent':
            session['pre'] = {**session['pre'], 'method': 'email'}
        flash({'sent': 'A new code is on its way.', 'wait': 'A code was sent a moment ago. Wait a minute before asking for another.',
               'failed': 'We could not email the code. Please try again in a minute.'}[result])
    return redirect(url_for('main.admin_2fa'))


# ---------------------------------------------------------------- admin sign-up, email proof, owner approval

def purge_unverified():
    """Drop sign-ups that never proved their email within a day, which frees the IDs and addresses they held."""
    Admin.query.filter(Admin.status == 'unverified', Admin.created_at < utcnow() - timedelta(hours=24)).delete(synchronize_session=False)
    db.session.commit()


@bp.route('/admin/signup', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 20 per day', methods=['POST'])
def admin_signup():
    if not signup_open():
        return render_template('signup.html', closed=True, form={}), 404
    form = {'username': request.form.get('username', '').strip(), 'email': request.form.get('email', '').strip()}
    if request.method == 'POST':
        purge_unverified()
        password = request.form.get('password', '')
        try:
            if password_problem(password, request.form.get('confirm', '')):
                raise ValueError(password_problem(password, request.form.get('confirm', '')))
            if not norm_email(form['email']):
                raise ValueError('Enter your email address: the portal sends a code to it.')
            admin, _ = create_admin(form['username'], password, form['email'], status='unverified')
        except ValueError as exc:
            flash(str(exc))
        except IntegrityError:  # two sign-ups with the same ID at the same moment
            db.session.rollback()
            flash('That ID was just taken. Please choose another.')
        else:
            sent = send_otp(admin, 'verify', admin.pending_email)
            set_pre(admin, 'verify')  # clears the session, so any message must be flashed after it
            if sent == 'failed':
                flash('We could not email the code. Use "Send a new code" in a minute.')
            return redirect(url_for('main.admin_verify'))
    return render_template('signup.html', closed=False, form=form)


@bp.route('/admin/verify', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def admin_verify():
    """Enter the emailed code to prove an address: at sign-up, or after changing the email under Account."""
    admin = pre_admin('verify') or current_admin()
    if not admin or not admin.pending_email:
        return redirect(url_for('main.admin_login'))
    if request.method == 'POST':
        lock_admin(admin)
        if check_otp(admin, 'verify', request.form.get('code', '')):
            return complete_verification(admin)
        db.session.commit()
        flash('That code is not right or has expired.')
    return render_template('verify.html', email=mask_email(admin.pending_email), signed_in=bool(current_admin()))


@bp.post('/admin/verify/resend')
@limiter.limit('5 per minute')
def admin_verify_resend():
    admin = pre_admin('verify') or current_admin()
    if not admin or not admin.pending_email:
        return redirect(url_for('main.admin_login'))
    lock_admin(admin)
    result = send_otp(admin, 'verify', admin.pending_email)
    flash({'sent': 'A new code is on its way.', 'wait': 'A code was sent a moment ago. Wait a minute before asking for another.',
           'failed': 'We could not email the code. Please try again in a minute.'}[result])
    return redirect(url_for('main.admin_verify'))


def complete_verification(admin):
    new, old = admin.pending_email, admin.email
    admin.email, admin.pending_email = new, None
    first_time = admin.status == 'unverified'
    if first_time:
        admin.status = 'pending'
    code = issue_recovery_code(admin) if first_time else None
    try:
        db.session.commit()
    except IntegrityError:  # someone proved the same address first
        db.session.rollback()
        admin = db.session.get(Admin, admin.id)
        admin.pending_email = None
        db.session.commit()
        flash('That email address already has an account.')
        return redirect(url_for('main.account' if current_admin() else 'main.admin_signup'))
    if first_time:
        session.clear()
        for owner in Admin.query.filter(Admin.is_owner.is_(True), Admin.status == 'active', Admin.email.isnot(None)):
            try:
                send_mail(owner.email, 'New administrator waiting for approval',
                          f'"{admin.username}" has signed up and confirmed their email address. '
                          f'Approve or reject them on the Admins page of the portal.')
            except mailer.MailError:
                pass  # the Admins page still shows them
        return render_template('recovery_done.html', code=code, pending=True)
    if old and old != new:
        try:
            send_mail(old, 'Your ORS/SRS portal email address was changed',
                      'The email address on your administrator account was just changed. If this was not you, '
                      'sign in and change your password, or ask the portal owner.')
        except mailer.MailError:
            pass
    flash('Your email address is confirmed. Two-step sign-in is on.')
    return redirect(url_for('main.account'))


@bp.get('/admin/admins')
@owner_required
def admins():
    rows = db.session.execute(
        select(Admin, func.count(Client.id)).outerjoin(Client, Client.admin_id == Admin.id).group_by(Admin.id)
        .order_by(case((Admin.status == 'pending', 0), else_=1), Admin.id)).all()
    return render_template('admins.html', rows=rows)


@bp.post('/admin/admins/<int:admin_id>/<any(approve,reject,disable,enable):action>')
@owner_required
def admin_action(admin_id, action):
    target = lock_admin(db.get_or_404(Admin, admin_id))  # two owners (or two clicks) cannot both approve and both send mail
    allowed = {'approve': ('pending',), 'reject': ('pending', 'unverified'), 'disable': ('active',), 'enable': ('disabled',)}[action]
    if target.status not in allowed or target.is_owner or target.id == g.admin.id:
        flash('That is not possible for this account.')
        return redirect(url_for('main.admins'))
    name, email, notice = target.username, target.email, None  # only a proven address is ever mailed: a stranger's must not get our notices
    if action == 'reject':
        if target.clients:  # never delete someone who holds client data
            flash('That administrator has clients, so they cannot be removed. Disable them instead.')
            return redirect(url_for('main.admins'))
        db.session.delete(target)
        notice = ('Your ORS/SRS portal sign-up was not approved', 'The portal owner did not approve your administrator sign-up.')
    else:
        target.status = {'approve': 'active', 'disable': 'disabled', 'enable': 'active'}[action]
        if action == 'approve':
            notice = ('Your ORS/SRS portal account is approved',
                      f'The portal owner approved your account. Sign in at {url_for("main.admin_login", _external=True)} '
                      f'with the ID "{name}" and a code we email you.')
    db.session.commit()
    flash(f'{name}: {action}d.' if action != 'reject' else f'{name}: removed.')
    if notice and email and mail_ready():
        try:
            send_mail(email, *notice)
        except mailer.MailError:
            flash('The notice email could not be sent.')
    return redirect(url_for('main.admins'))


@bp.route('/admin/forgot', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def admin_forgot():
    """Forgotten password: the ID plus the recovery code made under Account. The recovery code is never emailed (mail
    here only carries sign-in codes), and a reset does not remove the second step. A locked-out admin with no code uses
    `python app.py reset-password`."""
    if request.method == 'POST':
        form = request.form
        admin = Admin.query.filter_by(username=norm_username(form.get('username', ''))).first()
        stored = (admin.recovery_hash if admin else None) or '0' * 64
        code_ok = secrets.compare_digest(recovery_hash(form.get('code', '')), stored) and bool(admin and admin.recovery_hash)
        problem = password_problem(form.get('password', ''), form.get('confirm', ''))
        if not code_ok:
            flash('That ID and recovery code do not match.')
        elif problem:
            flash(problem)
        else:
            return render_template('recovery_done.html', code=reset_password(admin, form['password']))
    return render_template('forgot.html')


@bp.post('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('main.admin_login'))


def confirmed_by_password():
    """Account changes re-ask for the current password, so a borrowed browser session cannot take over the account."""
    if check_password_hash(g.admin.password_hash, request.form.get('current', '')):
        return True
    flash('Your current password was not correct. Nothing was changed.')
    return False


@bp.get('/admin/account')
@admin_required
def account():
    return render_template('account.html', admin=g.admin, keys=g.admin.api_keys, mail=mail_ready(), version=VERSION,
                           totp_on=bool(g.admin.totp_enabled_at))


@bp.post('/admin/account/password')
@admin_required
@limiter.limit('5 per minute')
def account_password():
    new = request.form.get('password', '')
    if confirmed_by_password():
        problem = password_problem(new, request.form.get('confirm', ''))
        if problem:
            flash(problem)
        else:
            g.admin.password_hash = generate_password_hash(new)
            clear_otp(g.admin)  # a code asked for under the old password is void
            db.session.commit()
            session['stamp'] = g.admin.stamp  # this browser stays signed in; every other one is signed out
            flash('Password changed. Any other signed-in browser was signed out.')
    return redirect(url_for('main.account'))


@bp.post('/admin/account/recovery')
@admin_required
@limiter.limit('5 per minute')
def account_recovery():
    if confirmed_by_password():
        code = issue_recovery_code(g.admin)
        db.session.commit()
        flash(code, 'recovery')
    return redirect(url_for('main.account'))


@bp.post('/admin/account/keys')
@admin_required
@limiter.limit('5 per minute')
def account_key_create():
    label = ' '.join(request.form.get('label', '').split())[:60]
    if not label:
        flash('Give the key a label, for example "laptop".')
    elif sum(1 for k in g.admin.api_keys if not k.revoked_at) >= 10:
        flash('You already have 10 active keys. Revoke one first.')
    elif confirmed_by_password():
        key = new_api_key(g.admin, label)
        db.session.commit()
        flash(key, 'apikey')
    return redirect(url_for('main.account'))


@bp.post('/admin/account/keys/<int:key_id>/revoke')
@admin_required
def account_key_revoke(key_id):
    key = ApiKey.query.filter_by(id=key_id, admin_id=g.admin.id).first_or_404()
    key.revoked_at = key.revoked_at or utcnow()
    db.session.commit()
    flash(f'API key "{key.label}" revoked.')
    return redirect(url_for('main.account'))


@bp.post('/admin/account/email')
@admin_required
@limiter.limit('5 per minute')
def account_email():
    """Add or change the email address. It only becomes the sign-in address once a code sent to it is entered."""
    new = norm_email(request.form.get('email', ''))
    if not mail_ready():
        flash('This server cannot send email, so an address cannot be confirmed.')
    elif confirmed_by_password():
        if email_problem(new):
            flash(email_problem(new))
        elif new == g.admin.email:
            flash('That is already your email address.')
        elif Admin.query.filter(Admin.email == new, Admin.id != g.admin.id).first():
            flash('That email address already has an account.')
        else:
            lock_admin(g.admin)
            g.admin.pending_email = new
            if send_otp(g.admin, 'verify', new) == 'failed':
                flash('We could not email the code. Use "Send a new code" in a minute.')
            return redirect(url_for('main.admin_verify'))
    return redirect(url_for('main.account'))


@bp.route('/admin/account/totp', methods=['GET', 'POST'])
@admin_required
@limiter.limit('5 per minute', methods=['POST'])
def account_totp():
    """Turn on the authenticator app. The secret is made and stored (sealed) on first view; it only counts once a code
    from the app is entered here together with the password."""
    admin, key = g.admin, current_app.config['SECRET_KEY']
    if admin.totp_enabled_at:
        flash('The authenticator app is already on.')
        return redirect(url_for('main.account'))
    if not admin.email:
        flash('Confirm an email address first: it is the way back in if you lose the phone.')
        return redirect(url_for('main.account'))
    secret = twofactor.unseal(admin.totp_secret, key) if admin.totp_secret else None
    if request.method == 'POST' and secret and confirmed_by_password():
        step = twofactor.verify(secret, request.form.get('code', ''))
        if step:
            admin.totp_enabled_at, admin.totp_last_step = utcnow(), step
            db.session.commit()
            flash('Authenticator app is on. Sign-in now asks for its code.')
            return redirect(url_for('main.account'))
        flash('That code is not right. Check the phone clock and try the next code.')
    if not secret:
        secret = twofactor.new_secret()
        admin.totp_secret = twofactor.seal(secret, key)
        db.session.commit()
    uri = twofactor.provisioning_uri(secret, admin.username)
    return render_template('totp.html', qr=twofactor.qr_data_uri(uri), key=' '.join(secret[i:i + 4] for i in range(0, len(secret), 4)))


@bp.post('/admin/account/totp/disable')
@admin_required
@limiter.limit('5 per minute')
def account_totp_disable():
    admin = g.admin
    if admin.totp_enabled_at and confirmed_by_password():
        secret = twofactor.unseal(admin.totp_secret, current_app.config['SECRET_KEY'])
        # With a readable secret the app's code is needed too; an unreadable one (SECRET_KEY changed) cannot be checked.
        if secret and not twofactor.verify(secret, request.form.get('code', ''), admin.totp_last_step):
            flash('That code is not right. Nothing was changed.')
        else:
            admin.totp_secret = admin.totp_enabled_at = None
            admin.totp_last_step = 0
            db.session.commit()
            flash('Authenticator app turned off. Sign-in uses the emailed code.')
    return redirect(url_for('main.account'))


# ---------------------------------------------------------------- admin: clients list + recent

def client_rows(clients):
    """Each client with its list Status, from the newest link (one query for the whole page)."""
    newest = {}
    for link in Link.query.filter(Link.client_pk.in_([c.id for c in clients])).order_by(Link.id):
        newest[link.client_pk] = link  # ascending ids: the last one written is the newest
    return [{'client': c, 'state': link_state(newest.get(c.id))} for c in clients]


@bp.get('/admin')
@admin_required
def dashboard():
    f = read_filters(request.args)
    page = db.paginate(client_select(f).order_by(*SORTS[f['sort']][1]()),
                       page=request.args.get('page', 1, type=int), per_page=PER_PAGE, error_out=False)
    rows = client_rows(page.items)
    chips = {}  # one removable chip per active filter, in the order they appear in the panel
    if f['q']:
        chips['q'] = f'Search: {f["q"]}'
    if f['attention']:
        chips['attention'] = 'Needs attention' if f['attention'] == '1' else 'No attention flag'
    if f['status']:
        chips['status'] = f'Status: {STATUS_LABELS[f["status"]]}'
    if f['band']:
        chips['band'] = 'Age: ' + ('13 to 17' if f['band'] == 'teen' else 'Adult')
    if f['flag']:
        chips['flag'] = f'Flag: {scoring.FLAG_INFO[f["flag"]][3]}'
    if f['sort'] != 'new':
        chips['sort'] = f'Sort: {SORTS[f["sort"]][0]}'
    active = {k: f[k] for k in chips}
    stats = {'total': count_clients(), 'attention': count_clients(attention='1'), 'waiting': count_clients(status='waiting')}
    return render_template('admin.html', rows=rows, page=page, f=f, chips=chips, active=active, stats=stats,
                           filter_count=len([k for k in chips if k != 'q']), statuses=STATUS_LABELS,
                           sorts={k: v[0] for k, v in SORTS.items()},
                           flag_options={c: info[3] for c, info in scoring.FLAG_INFO.items()})


def recent_page():
    kind = request.args.get('type') if request.args.get('type') in scoring.ITEMS else ''
    q = (Assessment.query.join(Client).filter(Client.admin_id == g.admin.id).options(selectinload(Assessment.client))
         .order_by(Assessment.created_at.desc(), Assessment.id.desc()))
    if kind:
        q = q.filter(Assessment.type == kind)  # not filter_by: after the join it would look at Client
    return kind, q.paginate(page=request.args.get('page', 1, type=int), per_page=PER_PAGE, error_out=False)


@bp.get('/admin/recent')
@admin_required
def recent():
    kind, page = recent_page()
    return render_template('recent.html', page=page, kind=kind)


@bp.get('/admin/caseload')
@admin_required
def caseload_page():
    """How this administrator's whole caseload is doing. The numbers come from SQL (caseload_stats and
    caseload_by_session) and cover only the signed-in administrator's clients."""
    stats = caseload_stats(g.admin.id)
    rows = caseload_by_session(g.admin.id)
    parts = [('csc', 'Reliable and clinically significant improvement', '▲✓', stats['csc']),
             ('up', 'Reliable improvement', '▲', stats['up']),
             ('flat', 'No reliable change', '●', stats['flat']),
             ('down', 'Reliable decline', '▼', stats['down'])]
    return render_template('caseload.html', stats=stats, parts=parts, empty=not stats['with_ors'],
                           bar=outcome_bar_svg([(c, label, n) for c, label, _g, n in parts], 'Clients by outcome since intake'),
                           trend=session_means_svg(rows, 'Average ORS and SRS by session number', scoring.SRS_CUTOFF),
                           min_n=scoring.EFFECT_SIZE_MIN_CLIENTS, rc=scoring.ORS_RELIABLE_CHANGE, srs_cutoff=scoring.SRS_CUTOFF,
                           pct=lambda n, d: round(100 * n / d) if d else 0)


@bp.post('/admin/clients')
@admin_required
def create_client():
    first = ' '.join(request.form.get('first_name', '').split())
    last = ' '.join(request.form.get('last_name', '').split())
    band = request.form.get('age_band', 'adult')
    problem = ('First name and last name are both required.' if not first or not last else
               'Names must be 80 characters or fewer.' if len(first) > 80 or len(last) > 80 else
               'Unknown age band.' if band not in scoring.ORS_CUTOFF else '')
    if problem:
        flash(problem)
        return redirect(url_for('main.dashboard', new=1))  # reopen the add-client drawer
    base = make_slug(first) + '-' + make_slug(last)
    for n in range(1, 1000):  # the UNIQUE constraint is the source of truth; no check-then-insert race
        candidate = base if n == 1 else f'{base}-{n}'
        db.session.add(Client(admin_id=g.admin.id, slug=candidate, first_name=first, last_name=last, age_band=band))
        try:
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
    else:
        abort(500)
    flash(f'Client created. ID: {candidate}. Create a session link below when you are ready.')
    return redirect(url_for('main.client_detail', slug=candidate))


# ---------------------------------------------------------------- admin: one client

def build_insight(client, sessions, series):
    """Everything the client page shows beyond the table: chart points, summary cards, domain changes. `series` is
    client_series(): the changes in the tooltips come from the database, the reliable-change rules from scoring.py."""
    by = {(r['no'], r['type']): r for r in series}
    band = client.age_band
    cutoff = scoring.ORS_CUTOFF[band]
    ors = [s for s in sessions if s['ORS']]
    srs = [s for s in sessions if s['SRS']]
    ors_points = []
    for i, s in enumerate(ors):
        v, first = s['ors_total'], ors[0]['ors_total']
        status = scoring.ors_status(band, first, v) if i else None
        tip = [f'Session {s["no"]} · {s["date"]}', f'ORS total {v:g} / 40']
        row = by[(s['no'], 'ORS')]
        if i:
            tip.append(f'{row["since_intake"]:+g} since intake' + {'csc': ' · reliable and clinically significant improvement',
                       'up': ' · reliable improvement', 'down': ' · reliable deterioration', None: ' · no reliable change'}[status])
            tip.append(f'{row["since_prev"]:+g} since previous session')
        else:
            tip.append('Intake (first ORS)')
        tip.append(f'{"Below" if v < cutoff else "At or above"} the cutoff of {cutoff}')
        if s['SRS']:  # the two scales are read together at each session, so show the alliance rating beside the outcome
            tip.append(f'SRS that session: {s["srs_total"]:g}' + (f' (below {scoring.SRS_CUTOFF})' if s['srs_total'] < scoring.SRS_CUTOFF else ''))
        ors_points.append({'no': s['no'], 'value': v, 'status': status, 'tip': '\n'.join(tip)})
    srs_points = []
    for s in srs:
        v, scores = s['srs_total'], s['SRS'].scores
        low = scoring.srs_low_items(scores)
        tip = [f'Session {s["no"]} · {s["date"]}',
               f'SRS total {v:g} / 40' + (f' · below {scoring.SRS_CUTOFF}' if v < scoring.SRS_CUTOFF else ''),
               ' · '.join(f'{k} {scores[k]:g}' for k in scoring.SRS_ITEMS)]
        if low:
            tip.append(f'Below {scoring.SRS_ITEM_FLOOR}: ' + ', '.join(low))
        change = by[(s['no'], 'SRS')]['since_prev']
        if change is not None:  # shown, never turned into a flag: no validated rule says how big a fall matters
            tip.append(f'{change:+g} since previous SRS')
        if s['ORS']:
            tip.append(f'ORS that session: {s["ors_total"]:g}')
        srs_points.append({'no': s['no'], 'value': v, 'status': None, 'ring': bool(low), 'tip': '\n'.join(tip)})
    summary = {'sessions': len(sessions), 'first': sessions[0]['date'] if sessions else None,
               'last': sessions[-1]['date'] if sessions else None,
               'days': (sessions[-1]['date'] - sessions[0]['date']).days if sessions else 0}
    if ors:
        first, latest = ors[0]['ors_total'], ors[-1]['ors_total']
        status = scoring.ors_status(band, first, latest) if len(ors) > 1 else None
        summary['ors'] = {'intake': first, 'latest': latest, 'delta': round(latest - first, 2), 'count': len(ors),
                          'above': latest >= cutoff, 'status': status,
                          'label': {'csc': 'Reliable and clinically significant improvement', 'up': 'Reliable improvement',
                                    'down': 'Reliable deterioration', None: 'No reliable change yet' if len(ors) > 1 else 'Intake only'}[status]}
    if srs:
        last_scores = srs[-1]['SRS'].scores
        summary['srs'] = {'latest': srs[-1]['srs_total'], 'count': len(srs), 'below': sum(1 for s in srs if s['srs_total'] < scoring.SRS_CUTOFF),
                          'mean': round(sum(s['srs_total'] for s in srs) / len(srs), 1), 'low_items': scoring.srs_low_items(last_scores)}
    domains = scoring.domain_changes('ORS', ors[0]['ORS'].scores, ors[-1]['ORS'].scores) if len(ors) > 1 else []
    domain_rows = [{'no': s['no'], 'date': s['date'], 'scores': s['ORS'].scores} for s in ors]
    charts = {'ors': trend_svg(ors_points, cutoff, 'ORS total by session',
                               band=(ors[0]['ors_total'] - scoring.ORS_RELIABLE_CHANGE, ors[0]['ors_total'] + scoring.ORS_RELIABLE_CHANGE) if ors else None),
              'srs': trend_svg(srs_points, scoring.SRS_CUTOFF, 'SRS total by session'),
              'domains': domains_svg(domain_rows, scoring.ORS_ITEMS, 'ORS domains by session')}
    return summary, domains, charts


@bp.get('/admin/client/<slug>')
@admin_required
def client_detail(slug):
    client = get_client(slug)
    sessions = sessions_of(client.assessments)
    summary, domains, charts = build_insight(client, sessions, client_series(client.id))
    link = latest_link(client)
    return render_template('client.html', client=client, sessions=sessions, flags=flags_for(client, sessions),
                           summary=summary, domains=domains, charts=charts, link_text=link_message(link),
                           state=activity_state(client), link_active=bool(link and link.status() == 'active'),
                           ors_cutoff=scoring.ORS_CUTOFF[client.age_band])


@bp.get('/admin/client/<slug>/activity.json')
def client_activity(slug):
    g.admin = current_admin()
    if not g.admin:
        abort(401)
    return jsonify(state=activity_state(get_client(slug)))


@bp.post('/admin/client/<slug>/links')
@admin_required
def create_link(slug):
    client = get_client(slug)
    kinds = request.form.get('kinds', 'ORS,SRS')
    if kinds not in ('ORS,SRS', 'ORS', 'SRS'):
        abort(400)
    lock_client(client.id)  # two clicks at once must leave exactly one live link (client.links loads after the lock)
    now = utcnow()
    for old in client.links:  # one live link per client; a new one replaces the old
        if old.status() == 'active':
            old.revoked_at = now
    token = secrets.token_urlsafe(24)
    db.session.add(Link(client_pk=client.id, token_hash=hash_token(token), kinds=kinds,
                        expires_at=now + timedelta(days=current_app.config['LINK_TTL_DAYS'])))
    db.session.commit()
    # shown once: only the hash is stored, so this is the admin's only chance to copy it
    flash(url_for('main.client_assessment', token=token, _external=True), 'link')
    return redirect(url_for('main.client_detail', slug=slug))


@bp.post('/admin/client/<slug>/links/revoke')
@admin_required
def revoke_links(slug):
    client = get_client(slug)
    for link in client.links:
        if link.status() == 'active':
            link.revoked_at = utcnow()
    db.session.commit()
    flash('Session link revoked.')
    return redirect(url_for('main.client_detail', slug=slug))


@bp.post('/admin/client/<slug>/delete')
@admin_required
def delete_client(slug):
    client = get_client(slug)
    db.session.delete(client)  # cascades to links and assessments in one transaction
    db.session.commit()
    flash(f'Deleted client {slug} and all associated data.')
    return redirect(url_for('main.dashboard'))


# ---------------------------------------------------------------- admin: assessments + reports

@bp.get('/admin/assessment/<int:assessment_id>')
@admin_required
def assessment_detail(assessment_id):
    a = get_assessment(assessment_id)
    low = set(scoring.srs_low_items(a.scores)) if a.type == 'SRS' else set()
    return render_template('assessment_detail.html', a=a, client=a.client, low=low)


@bp.post('/admin/assessment/<int:assessment_id>/delete')
@admin_required
def delete_assessment(assessment_id):
    a = get_assessment(assessment_id)
    client = a.client
    db.session.delete(a)
    db.session.flush()
    refresh_summary(client)
    db.session.commit()
    flash('Assessment deleted.')
    return redirect(url_for('main.client_detail', slug=client.slug))


@bp.get('/admin/client/<slug>/report')
@admin_required
def report(slug):
    client = get_client(slug)
    return render_template('report.html', client=client, tables=report_tables(client))


@bp.get('/admin/client/<slug>/export')
@admin_required
def export_client(slug):
    client = get_client(slug)
    return send_file(build_workbook(client), as_attachment=True, mimetype=XLSX,
                     download_name=f'{client.slug}_PCOMS_report.xlsx')


@bp.get('/admin/export-all')
@admin_required
def export_all():
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as z:
        for client in Client.query.filter_by(admin_id=g.admin.id).order_by(Client.id):
            z.writestr(f'{client.slug}_PCOMS_report.xlsx', build_workbook(client).getvalue())
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name='PCOMS_all_client_reports.zip', mimetype='application/zip')


# ---------------------------------------------------------------- read-only JSON API (per-administrator key)

def api_get(path):
    def decorate(view):
        return bp.get('/api/v1' + path)(limiter.limit('120 per minute')(api_required(view)))
    return decorate


@api_get('/me')
def api_me():
    return jsonify(username=g.admin.username, key={'label': g.api_key.label, 'prefix': g.api_key.prefix}, version=VERSION)


@api_get('/clients')
def api_clients():
    f = read_filters(request.args)
    page = db.paginate(client_select(f).order_by(*SORTS[f['sort']][1]()),
                       page=request.args.get('page', 1, type=int), per_page=PER_PAGE, error_out=False)
    return jsonify(total=page.total, page=page.page, pages=page.pages, clients=[{
        'id': c.slug, 'name': f'{c.first_name} {c.last_name}', 'age_band': c.age_band, 'status': state,
        'sessions': c.session_count, 'last_session': c.last_session_on and c.last_session_on.isoformat(),
        'ors_latest': c.ors_latest, 'srs_latest': c.srs_latest, 'needs_attention': c.needs_attention,
        'flags': [code for code in c.flag_codes.strip(',').split(',') if code]} for c, state in
        ((r['client'], r['state']) for r in client_rows(page.items))])


@api_get('/clients/<slug>')
def api_client(slug):
    client = get_client(slug)
    sessions = sessions_of(client.assessments)

    def part(a):
        return {'total': a.total, 'scores': dict(a.ordered_scores)} if a else None
    return jsonify(id=client.slug, name=f'{client.first_name} {client.last_name}', age_band=client.age_band,
                   needs_attention=client.needs_attention,
                   flags=[{'code': x['code'], 'level': x['level'], 'text': x['text']} for x in flags_for(client, sessions)],
                   sessions=[{'no': x['no'], 'date': x['date'].isoformat(), 'ors': part(x['ORS']), 'srs': part(x['SRS'])}
                             for x in sessions])


@api_get('/recent')
def api_recent():
    kind, page = recent_page()
    return jsonify(total=page.total, page=page.page, pages=page.pages, type=kind or None, assessments=[{
        'id': a.id, 'client': a.client.slug, 'type': a.type, 'session': a.session_no, 'date': a.taken_on.isoformat(),
        'total': a.total} for a in page.items])


@bp.after_app_request
def security_headers(resp):
    h = resp.headers
    h.setdefault('Content-Security-Policy', CSP)  # no inline script or style anywhere: all code is served from /static
    h.setdefault('X-Frame-Options', 'DENY')
    h.setdefault('Referrer-Policy', 'no-referrer')  # keeps the link token out of Referer
    h.setdefault('X-Robots-Tag', 'noindex, nofollow')
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if request.is_secure:
        h.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if request.path.startswith(('/admin', '/client', '/api')):
        h['Cache-Control'] = 'no-store'
    return resp


# ---------------------------------------------------------------- local settings file (.env, git-ignored)

ENV_FILE = BASE / '.env'


def read_env(path=None):
    """KEY=VALUE lines of a .env file as a dict. Blank lines and # comments are skipped; one pair of quotes is stripped."""
    path = path or ENV_FILE
    out = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = (part.strip() for part in line.split('=', 1))
            out[key] = value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in '\'"' else value
    return out


def load_env(path=None):
    """Copy .env into the process environment. A variable already set (Render, your shell) wins over the file."""
    for key, value in read_env(path).items():
        os.environ.setdefault(key, value)


def write_env(updates, path=None, remove=()):
    """Set keys in a .env file, drop the `remove` keys, keep every other line. Values are single-quoted so a shell
    that sources the file reads any `$` literally."""
    path = path or ENV_FILE
    lines = [ln for ln in (path.read_text().splitlines() if path.exists() else []) if ln.split('=', 1)[0].strip() not in remove]
    for key, value in updates.items():
        new = f"{key}='{value}'"
        at = next((i for i, line in enumerate(lines) if line.split('=', 1)[0].strip() == key), None)
        if at is None:
            lines.append(new)
        else:
            lines[at] = new
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only when the file is created
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def setup_env(path=None):
    """Local settings file: a session secret (kept if present) and ORS_SRS_ENV=dev, which lets http://localhost login work.
    Logins live in the database, not here."""
    old = read_env(path)
    write_env({'ORS_SRS_SECRET_KEY': old.get('ORS_SRS_SECRET_KEY') or secrets.token_hex(32),
               'ORS_SRS_ENV': old.get('ORS_SRS_ENV', 'dev')}, path)


def ask_email():
    """Optional: the address that will receive sign-in codes once it is confirmed in the portal."""
    email = norm_email(input('Email address for sign-in codes (optional, press Enter to skip): '))
    if email and email_problem(email):
        sys.exit(email_problem(email) + ' Nothing changed.')
    return email or None


def ask_password():
    import getpass
    password = getpass.getpass(f'New administrator password ({MIN_PASSWORD_LEN}+ characters): ')
    if password_problem(password):
        sys.exit(password_problem(password) + ' Nothing changed.')
    if password != getpass.getpass('Repeat it: '):
        sys.exit('The two entries differ. Nothing changed.')
    return password


# ---------------------------------------------------------------- app factory

def database_url(url):
    if not url:
        (BASE / 'data').mkdir(exist_ok=True)
        return 'sqlite:///' + str(BASE / 'data' / 'app.db')
    for prefix in ('postgres://', 'postgresql://'):  # hosts hand out these; SQLAlchemy needs the driver named
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url[len(prefix):]
    return url


def create_app(config=None):
    app = Flask(__name__)
    env = os.environ.get
    app.config.update(
        SECRET_KEY=env('ORS_SRS_SECRET_KEY'),
        ADMIN_USERNAME=env('ORS_SRS_ADMIN_USERNAME', 'admin'),
        ADMIN_PASSWORD_HASH=env('ORS_SRS_ADMIN_PASSWORD_HASH'),
        # Neon (`neon env pull`) writes a pooled DATABASE_URL and a direct DATABASE_URL_UNPOOLED. This app keeps one long-lived
        # SQLAlchemy pool and runs create_all (DDL) at start, which Neon advises doing over the direct string.
        SQLALCHEMY_DATABASE_URI=database_url(env('DATABASE_URL_UNPOOLED') or env('DATABASE_URL')),
        SQLALCHEMY_ENGINE_OPTIONS={'pool_pre_ping': True, 'pool_recycle': 300},  # serverless Postgres drops idle connections
        TIMEZONE=env('ORS_SRS_TIMEZONE', 'UTC'),
        TEMPLATE_FILE=env('PCOMS_TEMPLATE', str(BASE / 'PCOMS_template.xlsx')),
        LINK_TTL_DAYS=7,
        SESSION_COOKIE_SECURE=env('ORS_SRS_ENV') != 'dev',  # set ORS_SRS_ENV=dev to log in over plain http locally
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=64 * 1024,
        # Flask-WTF would also demand a Referer on every HTTPS POST, but Referrer-Policy: no-referrer (which keeps link
        # tokens out of Referer) means browsers send none. The CSRF token itself is still checked on every POST.
        WTF_CSRF_SSL_STRICT=False,
        REQUIRE_ADMIN=True,  # False only for the admin commands that may run before any administrator exists
        # Mail carries one-time codes only. Unset means no sign-up and no emailed codes (see mailer.py; Render's free plan
        # blocks SMTP, so there use ORS_SRS_MAIL=brevo).
        MAIL_BACKEND=env('ORS_SRS_MAIL', ''),
        MAIL_FROM=env('ORS_SRS_MAIL_FROM', ''),
        SMTP_HOST=env('ORS_SRS_SMTP_HOST', ''),
        SMTP_PORT=env('ORS_SRS_SMTP_PORT', ''),
        SMTP_USER=env('ORS_SRS_SMTP_USER', ''),
        SMTP_PASSWORD=env('ORS_SRS_SMTP_PASSWORD', ''),
        SMTP_SECURITY=env('ORS_SRS_SMTP_SECURITY', 'starttls'),
        BREVO_API_KEY=env('ORS_SRS_BREVO_API_KEY', ''),
        SIGNUP_ENABLED=env('ORS_SRS_SIGNUP', 'on').lower() != 'off',  # lets the owner close sign-up without touching mail
    )
    if config:
        app.config.update(config)
    if len(app.config['SECRET_KEY'] or '') < 16:
        raise RuntimeError('ORS_SRS_SECRET_KEY must be set to a random string of at least 16 characters.')
    mailer.check_config(app.config, dev=env('ORS_SRS_ENV') == 'dev')
    if app.config['MAIL_BACKEND'] == 'memory':
        app.config.setdefault('MAIL_OUTBOX', [])  # tests read the messages here
    hops = int(env('ORS_SRS_PROXY_HOPS', '0'))
    if hops:  # only trust X-Forwarded-* when actually behind that many proxies
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
    app.jinja_env.globals['min_len'] = MIN_PASSWORD_LEN
    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    app.register_blueprint(bp)
    @app.template_global()
    def filter_url(active, drop=None, **extra):
        """Clients-page URL with the active filters, minus `drop`, plus `extra` (for example page=2)."""
        return url_for('main.dashboard', **{**{k: v for k, v in active.items() if k != drop}, **extra})

    @app.context_processor
    def nav_extras():
        """The owner's nav shows how many sign-ups wait for approval."""
        admin = getattr(g, 'admin', None)
        if admin and admin.is_owner:
            return {'pending_admins': Admin.query.filter_by(status='pending').count()}
        return {}

    @app.template_filter('local')
    def local(dt):
        return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(app.config['TIMEZONE'])).strftime('%Y-%m-%d %H:%M') if dt else '—'

    with app.app_context():
        # ponytail: create_all only creates missing tables. v0.1.1 changed the schema with no migration because no database
        # was deployed yet; adopt Alembic before the next schema change.
        db.create_all()
        have = inspect(db.engine)
        missing = {t.name: sorted({c.name for c in t.columns} - {c['name'] for c in have.get_columns(t.name)}) for t in db.metadata.sorted_tables}
        missing = {t: cols for t, cols in missing.items() if cols}
        if missing:  # an older database: fail loudly here rather than with a confusing error on the first request
            raise RuntimeError(f'The database was created by an older version and lacks columns {missing}. Local dev: move the file '
                               'data/app.db aside (it may hold client data) or restore a backup made by this version. '
                               'Production: back up, then migrate (see DEPLOY.md, "Updating the app").')
        seed_admin(app.config['REQUIRE_ADMIN'])
    return app


# ---------------------------------------------------------------- command line

USAGE = """Commands (python app.py <command>):
  (none)                      start the app on http://127.0.0.1:5000
  setup                       first-time local setup: .env settings and your administrator login
  create-admin <id>           add another administrator
  reset-password <id>         set a new password (also signs the admin out everywhere and revokes their API keys)
  admins                      list administrators (no secrets are shown)
  reset-2fa <id>              remove an administrator's email and authenticator app (lost phone and mailbox)
  api-key <id> [label]        issue an API key for an administrator; shown once
  cli me|clients|client <id>|recent   read a running portal with ORS_SRS_API_KEY (see README)
  hash-password               print a password hash to paste into a host's settings (first start only)
  rebuild-summaries           recompute the client list's summary columns"""


def admin_app():
    """App context for the admin commands. They never touch cookies, so a missing SECRET_KEY is no obstacle
    (this lets you run them from your computer against the production DATABASE_URL alone)."""
    return create_app({'REQUIRE_ADMIN': False, 'SECRET_KEY': os.environ.get('ORS_SRS_SECRET_KEY') or secrets.token_hex(32)}).app_context()


def one_time(label, secret):
    print(f'\n{label} (shown only once; store it somewhere safe):\n    {secret}\n')


def command_setup():
    username = norm_username(input('Administrator ID [admin]: ') or 'admin')
    email = ask_email()
    password = ask_password()
    setup_env()
    load_env()
    os.environ.pop('ORS_SRS_ADMIN_PASSWORD_HASH', None)  # an old .env hash must not seed a second administrator
    with admin_app():
        admin = Admin.query.filter_by(username=username).first()
        if admin:
            code = reset_password(admin, password)
            if email:
                admin.pending_email = email
                db.session.commit()
        else:
            code = create_admin(username, password, email)[1]
    write_env({}, remove=('ORS_SRS_ADMIN_USERNAME', 'ORS_SRS_ADMIN_PASSWORD_HASH'))  # superseded by the database
    print(f'Saved administrator "{username}" in the database and settings in {ENV_FILE}.\n'
          f'Sign in with exactly that ID (it is not "admin" unless you chose that).')
    one_time('Recovery code (use it at /admin/forgot if you forget the password)', code)
    if email:
        print('Your email is saved but not yet confirmed. Set ORS_SRS_MAIL in .env (README, "Email"), sign in, and\n'
              'enter the code the portal sends: after that every sign-in asks for a code.\n')
    print('Start the app with: python app.py')


def command_create_admin(args):
    if not args:
        sys.exit('Usage: python app.py create-admin <id>')
    email = ask_email()
    with admin_app():
        admin, code = create_admin(args[0], ask_password(), email)
        name = admin.username
    print(f'Created administrator "{name}".')
    one_time('Recovery code', code)


def command_reset_password(args):
    if not args:
        sys.exit('Usage: python app.py reset-password <id>')
    with admin_app():
        admin = Admin.query.filter_by(username=norm_username(args[0])).first()
        if not admin:
            sys.exit(f'No administrator "{args[0]}". Run `python app.py admins` to see the IDs.')
        name, code = admin.username, reset_password(admin, ask_password())
    print(f'Password changed for "{name}". Their signed-in browsers and API keys were revoked.')
    one_time('New recovery code', code)


def command_admins():
    with admin_app():
        for a in Admin.query.order_by(Admin.id):
            second = 'authenticator app' if a.totp_enabled_at else ('email code' if a.email else 'none (password only)')
            print(f'{a.username}{" (owner)" if a.is_owner else ""}  {a.status}  created {a.created_at:%Y-%m-%d}  last sign-in '
                  f'{a.last_login_at.strftime("%Y-%m-%d %H:%M") if a.last_login_at else "never"} UTC  '
                  f'second step: {second}  clients {len(a.clients)}  '
                  f'recovery code {"set" if a.recovery_hash else "NOT set"}  '
                  f'active API keys {sum(1 for k in a.api_keys if not k.revoked_at)}')
        if not Admin.query.first():
            print('No administrators yet. Run `python app.py setup`.')


def command_reset_2fa(args):
    """The way back in after losing the phone and the mailbox: needs shell access to the host, which is the point."""
    if not args:
        sys.exit('Usage: python app.py reset-2fa <id>')
    with admin_app():
        admin = Admin.query.filter_by(username=norm_username(args[0])).first()
        if not admin:
            sys.exit(f'No administrator "{args[0]}". Run `python app.py admins` to see the IDs.')
        admin.email = admin.pending_email = admin.totp_secret = admin.totp_enabled_at = None
        admin.totp_last_step, admin.mfa_fails, admin.locked_until = 0, 0, None
        clear_otp(admin)
        db.session.commit()
        name = admin.username
    print(f'Second step removed for "{name}": they sign in with the password alone until they add and confirm an email '
          f'address under Account again.')


def command_api_key(args):
    if not args:
        sys.exit('Usage: python app.py api-key <id> [label]')
    with admin_app():
        admin = Admin.query.filter_by(username=norm_username(args[0])).first()
        if not admin:
            sys.exit(f'No administrator "{args[0]}".')
        name, key = admin.username, new_api_key(admin, ' '.join(args[1:])[:60] or 'cli')
        db.session.commit()
    one_time(f'API key for "{name}"', key)
    print('Use it with: export ORS_SRS_API_KEY=<the key>; python app.py cli clients')


def print_table(headers, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    for row in [headers, *rows]:
        print('  '.join(str(x).ljust(w) for x, w in zip(row, widths)).rstrip())


def command_cli(args):
    """Read the portal from the terminal through its JSON API, using an API key (never the secret key)."""
    import urllib.error
    import urllib.parse
    import urllib.request
    raw = '--json' in args
    args = [a for a in args if a != '--json'] or ['me']
    key = os.environ.get('ORS_SRS_API_KEY')
    if not key:
        sys.exit('Set ORS_SRS_API_KEY to your API key (Account page, or `python app.py api-key <id>`).')
    base = os.environ.get('ORS_SRS_URL', 'http://127.0.0.1:5000').rstrip('/')
    if not base.startswith(('http://', 'https://')):  # urlopen would also read file: and ftp: URLs
        sys.exit('ORS_SRS_URL must start with http:// or https://')
    what = args[0]
    if what == 'client' and len(args) == 2:
        path, params = '/clients/' + urllib.parse.quote(args[1], safe=''), []
    elif what in ('me', 'clients', 'recent'):
        path, params = '/' + what, [tuple(a.split('=', 1)) for a in args[1:] if '=' in a]
    else:
        sys.exit('Usage: python app.py cli me | clients [key=value ...] | client <id> | recent [key=value ...]  [--json]')
    url = base + '/api/v1' + path + ('?' + urllib.parse.urlencode(params) if params else '')
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key}), timeout=20) as r:  # nosec B310: scheme checked above
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get('error', e.reason)
        except ValueError:
            detail = e.reason
        sys.exit(f'{e.code}: {detail}')
    except urllib.error.URLError as e:
        sys.exit(f'Cannot reach {base}: {e.reason}. Is the app running?')
    if raw:
        print(json.dumps(data, indent=2))
        return
    dash = lambda v: '-' if v is None else v
    if what == 'me':
        print(f'Signed in to {base} as "{data["username"]}" with key "{data["key"]["label"]}" ({data["key"]["prefix"]}...)')
    elif what == 'clients':
        print(f'{data["total"]} clients, page {data["page"]} of {data["pages"]}')
        print_table(['ID', 'STATUS', 'SESSIONS', 'LAST', 'ORS', 'SRS', 'ATTENTION', 'FLAGS'],
                    [[c['id'], c['status'], c['sessions'], dash(c['last_session']), dash(c['ors_latest']), dash(c['srs_latest']),
                      'yes' if c['needs_attention'] else '', ','.join(c['flags'])] for c in data['clients']])
    elif what == 'client':
        print(f'{data["name"]} ({data["id"]}, {data["age_band"]}){"  NEEDS ATTENTION" if data["needs_attention"] else ""}')
        for f in data['flags']:
            print(f'  [{f["level"]}] {f["text"]}')
        print_table(['SESSION', 'DATE', 'ORS', 'SRS'], [[x['no'], x['date'], dash(x['ors'] and x['ors']['total']),
                                                         dash(x['srs'] and x['srs']['total'])] for x in data['sessions']])
    else:
        print(f'{data["total"]} assessments, page {data["page"]} of {data["pages"]}')
        print_table(['ID', 'CLIENT', 'TYPE', 'SESSION', 'DATE', 'TOTAL'],
                    [[a['id'], a['client'], a['type'], a['session'], a['date'], a['total']] for a in data['assessments']])


def command_rebuild_summaries():  # repair the client list's summary columns after any manual data fix
    with admin_app():
        for c in Client.query:
            refresh_summary(c)
        db.session.commit()
        print(f'Rebuilt {Client.query.count()} client summaries.')


if __name__ == '__main__':
    load_env()  # only here, never in create_app: tests and Render must not pick up a developer's .env
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ('', [])
    try:
        if cmd == '':
            create_app().run(host='127.0.0.1', port=5000, debug=False)
        elif cmd == 'setup':
            command_setup()
        elif cmd == 'create-admin':
            command_create_admin(args)
        elif cmd == 'reset-password':
            command_reset_password(args)
        elif cmd == 'admins':
            command_admins()
        elif cmd == 'reset-2fa':
            command_reset_2fa(args)
        elif cmd == 'api-key':
            command_api_key(args)
        elif cmd == 'cli':
            command_cli(args)
        elif cmd == 'hash-password':  # for a host such as Render, where you paste the hash into its settings
            print(generate_password_hash(ask_password()))
        elif cmd == 'rebuild-summaries':
            command_rebuild_summaries()
        else:
            sys.exit(USAGE)
    except ValueError as e:
        sys.exit(str(e))
