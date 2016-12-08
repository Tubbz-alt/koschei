# Copyright (C) 2014-2016  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Author: Michael Simacek <msimacek@redhat.com>

from __future__ import print_function, absolute_import

import logging

from functools import wraps

from flask import Flask, abort, request, g, url_for
from flask_sqlalchemy import BaseQuery, Pagination
from sqlalchemy.orm import scoped_session, sessionmaker

from koschei.session import KoscheiSession
from koschei.config import get_config, load_config
from koschei.db import Query, get_engine

dirs = get_config('directories')
app = Flask('koschei', template_folder=dirs['templates'],
            static_folder=dirs['static_folder'],
            static_url_path=dirs['static_url'])
app.config.update(get_config('flask'))

frontend_config = get_config('frontend')


class FrontendQuery(Query, BaseQuery):
    # pylint:disable=arguments-differ
    def paginate(self, items_per_page):
        try:
            page = int(request.args.get('page', 1))
        except ValueError:
            abort(400)
        if page < 1:
            abort(404)
        items = self.limit(items_per_page)\
                    .offset((page - 1) * items_per_page).all()
        if not items and page != 1:
            abort(404)
        if page == 1 and len(items) < items_per_page:
            total = len(items)
        else:
            total = self.order_by(None).count()
        return Pagination(self, page, items_per_page, total, items)

db = scoped_session(sessionmaker(autocommit=False, bind=get_engine(),
                                 query_cls=FrontendQuery))


class KoscheiFrontendSession(KoscheiSession):
    db = db
    log = logging.getLogger('koschei.frontend')


session = KoscheiFrontendSession()


tabs = []


class Tab(object):
    def __init__(self, name, order=0, requires_user=False):
        self.name = name
        self.order = order
        self.requires_user = requires_user
        self.master_endpoint = None
        for i, tab in enumerate(tabs):
            if tab.order > order:
                tabs.insert(i, self)
                break
        else:
            tabs.append(self)

    def __call__(self, fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            g.current_tab = self
            return fn(*args, **kwargs)
        return decorated

    def master(self, fn):
        self.master_endpoint = fn
        return self(fn)

    @property
    def url(self):
        name = self.master_endpoint.__name__
        if self.requires_user:
            return url_for(name, username=g.user.name)
        return url_for(name)

    @staticmethod
    def get_tabs():
        return [t for t in tabs if t.master_endpoint]


app.jinja_env.globals['get_tabs'] = Tab.get_tabs
