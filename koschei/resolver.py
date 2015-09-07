# Copyright (C) 2014-2015  Red Hat, Inc.
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
# Author: Mikolaj Izdebski <mizdebsk@redhat.com>

import re
import os
import time
import hawkey
import itertools
import librepo

from sqlalchemy.orm import joinedload

from koschei.models import (Package, Dependency, UnappliedChange,
                            AppliedChange, Repo, ResolutionProblem,
                            RepoGenerationRequest, Build, BuildrootProblem)
from koschei import util
from koschei.service import KojiService
from koschei.repo_cache import RepoCache
from koschei.backend import check_package_state, Backend


def get_best_selector(sack, dep):
    # Based on get_best_selector in dnf's subject.py

    def is_glob_pattern(pattern):
        return set(pattern) & set("*[?")

    def first(iterable):
        it = iter(iterable)
        try:
            return next(it)
        except StopIteration:
            return None

    def _nevra_to_selector(sltr, nevra):
        if nevra.name is not None:
            if is_glob_pattern(nevra.name):
                sltr.set(name__glob=nevra.name)
            else:
                sltr.set(name=nevra.name)
        if nevra.version is not None:
            evr = nevra.version
            if nevra.epoch is not None and nevra.epoch > 0:
                evr = "%d:%s" % (nevra.epoch, evr)
            if nevra.release is None:
                sltr.set(version=evr)
            else:
                evr = "%s-%s" % (evr, nevra.release)
                sltr.set(evr=evr)
        if nevra.arch is not None:
            sltr.set(arch=nevra.arch)
        return sltr

    subj = hawkey.Subject(dep)
    sltr = hawkey.Selector(sack)
    kwargs = {'allow_globs': True}
    if re.search(r'^\*?/', dep):
        key = "file__glob" if is_glob_pattern(dep) else "file"
        return sltr.set(**{key: dep})
    nevra = first(subj.nevra_possibilities_real(sack, **kwargs))
    if nevra:
        return _nevra_to_selector(sltr, nevra)

    if is_glob_pattern(dep):
        return sltr.set(provides__glob=dep)

    # pylint: disable=E1101
    reldep = first(subj.reldep_possibilities_real(sack))
    if reldep:
        dep = str(reldep)
        return sltr.set(provides=dep)
    return sltr


class AbstractResolverTask(object):
    def __init__(self, log, db, koji_session,
                 repo_cache, backend):
        self.log = log
        self.db = db
        self.koji_session = koji_session
        self.repo_cache = repo_cache
        self.backend = backend
        self.problems = []
        self.sack = None
        self.group = None
        self.resolved_packages = {}

    def run_goal(self, br=()):
        # pylint:disable=E1101
        goal = hawkey.Goal(self.sack)
        problems = []
        for name in self.group:
            sltr = hawkey.Selector(self.sack).set(name=name)
            if not sltr.matches():
                problems.append("Package in base build group not found: {}".format(name))
            goal.install(select=sltr)
        for r in br:
            sltr = get_best_selector(self.sack, r)
            # pylint: disable=E1103
            if not sltr.matches():
                problems.append("No package found for: {}".format(r))
            else:
                goal.install(select=sltr)
        if not problems:
            resolved = goal.run()
            return resolved, goal.problems, goal.list_installs() if resolved else None
        return False, problems, None

    def store_deps(self, repo_id, package_id, installs):
        new_deps = []
        for install in installs or []:
            if install.arch != 'src':
                dep = Dependency(repo_id=repo_id, package_id=package_id,
                                 name=install.name, epoch=install.epoch,
                                 version=install.version,
                                 release=install.release,
                                 arch=install.arch)
                new_deps.append(dep)

        if new_deps:
            # pylint: disable=E1101
            table = Dependency.__table__
            dicts = [{c.name: getattr(dep, c.name) for c in table.c
                      if not c.primary_key}
                     for dep in new_deps]
            self.db.connection().execute(table.insert(), dicts)
            self.db.expire_all()

    def compute_dependency_distances(self, br, deps):
        dep_map = {dep.name: dep for dep in deps}
        visited = set()
        level = 1
        # pylint:disable=E1103
        pkgs_on_level = {x for r in br for x in
                         get_best_selector(self.sack, r).matches()}
        while pkgs_on_level:
            for pkg in pkgs_on_level:
                dep = dep_map.get(pkg.name)
                if dep and dep.distance is None:
                    dep.distance = level
            level += 1
            if level >= 5:
                break
            reldeps = {req for pkg in pkgs_on_level if pkg not in visited
                       for req in pkg.requires}
            visited.update(pkgs_on_level)
            pkgs_on_level = set(hawkey.Query(self.sack).filter(provides=reldeps))

    def resolve_dependencies(self, package, br):
        resolved, problems, installs = self.run_goal(br)
        self.resolved_packages[package.id] = resolved
        if resolved:
            deps = [Dependency(name=pkg.name, epoch=pkg.epoch,
                               version=pkg.version, release=pkg.release,
                               arch=pkg.arch)
                    for pkg in installs if pkg.arch != 'src']
            self.compute_dependency_distances(br, deps)
            return deps
        else:
            for problem in sorted(set(problems)):
                entry = dict(package_id=package.id,
                             problem=problem)
                self.problems.append(entry)

    def get_deps_from_db(self, package_id, repo_id):
        deps = self.db.query(Dependency)\
                      .filter_by(repo_id=repo_id,
                                 package_id=package_id)
        return deps.all()

    def create_dependency_changes(self, deps1, deps2, **rest):
        if not deps1 or not deps2:
            # TODO packages with no deps
            return []

        def key(dep):
            return (dep.name, dep.epoch, dep.version, dep.release)

        def new_change(**values):
            change = dict(prev_version=None, prev_epoch=None,
                          prev_release=None, curr_version=None,
                          curr_epoch=None, curr_release=None)
            change.update(rest)
            change.update(values)
            return change

        old = util.set_difference(deps1, deps2, key)
        new = util.set_difference(deps2, deps1, key)

        changes = {}
        for dep in old:
            change = new_change(dep_name=dep.name,
                                prev_version=dep.version, prev_epoch=dep.epoch,
                                prev_release=dep.release, distance=dep.distance)
            changes[dep.name] = change
        for dep in new:
            change = changes.get(dep.name) or new_change(dep_name=dep.name)
            change.update(curr_version=dep.version, curr_epoch=dep.epoch,
                          curr_release=dep.release, distance=dep.distance)
            changes[dep.name] = change
        return changes.values() if changes else []

    def get_prev_build_for_comparison(self, build):
        return self.db.query(Build)\
                      .filter_by(package_id=build.package_id)\
                      .filter(Build.id < build.id)\
                      .filter(Build.deps_resolved == True)\
                      .order_by(Build.id.desc()).first()

    def prepare_sack(self, repo_id):
        for_arch = util.config['dependency']['for_arch']
        sack = hawkey.Sack(arch=for_arch)
        repos = self.repo_cache.get_repos(repo_id)
        self.log.info("Loading repos into sack...")
        if repos:
            util.add_repos_to_sack(repo_id, repos, sack)
            self.sack = sack


class GenerateRepoTask(AbstractResolverTask):

    def get_packages(self, expunge=True, require_build=False):
        query = self.db.query(Package).filter(Package.blocked == False)\
                       .filter(Package.tracked == True)
        if require_build:
            query = query.filter(Package.last_complete_build_id != None)
        packages = query.options(joinedload(Package.last_build))\
                        .options(joinedload(Package.last_complete_build))\
                        .all()
        # detaches objects from ORM, prevents spurious queries that hinder
        # performance
        if expunge:
            for p in packages:
                self.db.expunge(p)
        return packages

    def update_repo_index(self, repo_id):
        index_path = os.path.join(util.config['directories']['repodata'], 'index')
        with open(index_path, 'w') as index:
            index.write('{}\n'.format(repo_id))

    def synchronize_resolution_state(self):
        packages = self.get_packages(expunge=False)
        for pkg in packages:
            curr_state = self.resolved_packages.get(pkg.id)
            if curr_state is not None:
                prev_state = pkg.msg_state_string
                pkg.resolved = curr_state
                check_package_state(pkg, prev_state)

        for state in True, False:
            ids = [id for id, resolved in self.resolved_packages.iteritems()
                   if resolved is state]
            if ids:
                self.db.query(Package).filter(Package.id.in_(ids))\
                    .update({'resolved': state}, synchronize_session=False)

    def get_build_for_comparison(self, package):
        """
        Returns newest build which should be used for dependency
        comparisons or None if it shouldn't be compared at all
        """
        last_build = package.last_build
        if last_build and last_build.state in Build.FINISHED_STATES:
            if last_build.deps_resolved:
                return last_build
            if last_build.deps_processed:
                # unresolved build, skip it
                return self.get_prev_build_for_comparison(last_build)

    def generate_dependency_changes(self, packages, brs, repo_id):
        changes = []
        for package, br in zip(packages, brs):
            curr_deps = self.resolve_dependencies(package, br)
            if curr_deps is not None:
                last_build = self.get_build_for_comparison(package)
                if last_build:
                    prev_deps = self.get_deps_from_db(last_build.package_id,
                                                      last_build.repo_id)
                    if prev_deps is not None:
                        changes += self.create_dependency_changes(
                            prev_deps, curr_deps, package_id=package.id,
                            prev_build_id=last_build.id)
        return changes

    def update_dependency_changes(self, changes):
        self.db.query(UnappliedChange).delete(synchronize_session=False)
        if changes:
            self.db.execute(UnappliedChange.__table__.insert(), changes)

    def run(self, repo_id):
        start = time.time()
        self.log.info("Generating new repo")
        self.log.info("Polling latest real builds")
        self.backend.refresh_latest_builds()
        self.db.commit()
        packages = self.get_packages(require_build=True)
        repo = Repo(repo_id=repo_id)
        self.prepare_sack(repo_id)
        if not self.sack:
            self.log.error('Cannot generate repo: {}'.format(repo_id))
            self.db.rollback()
            return
        self.update_repo_index(repo_id)
        # TODO repo_id
        self.group = util.get_build_group(self.koji_session)
        base_installable, base_problems, _ = self.run_goal()
        if not base_installable:
            self.log.info("Build group not resolvable")
            repo.base_resolved = False
            self.db.add(repo)
            self.db.flush()
            self.db.execute(BuildrootProblem.__table__.insert(),
                            [{'repo_id': repo.repo_id, 'problem': problem}
                             for problem in base_problems])
            self.db.commit()
            return
        brs = util.get_rpm_requires(self.koji_session,
                                    [dict(name=p.name,
                                          version=p.last_complete_build.version,
                                          release=p.last_complete_build.release,
                                          arch='src') for p in packages])
        self.log.info("Resolving dependencies...")
        resolution_start = time.time()
        changes = self.generate_dependency_changes(packages, brs, repo_id)
        resolution_end = time.time()
        self.db.query(ResolutionProblem).delete(synchronize_session=False)
        # pylint: disable=E1101
        if self.problems:
            self.db.execute(ResolutionProblem.__table__.insert(), self.problems)
        self.synchronize_resolution_state()
        self.update_dependency_changes(changes)
        repo.base_resolved = True
        self.db.add(repo)
        self.db.commit()
        end = time.time()

        self.log.info(("New repo done. Resolution time: {} minutes\n"
                       "Overall time: {} minutes.")
                      .format((resolution_end - resolution_start) / 60,
                              (end - start) / 60))

class ProcessBuildsTask(AbstractResolverTask):

    def process_build(self, build, br):
        self.log.info("Processing build {}".format(build.id))
        prev = self.get_prev_build_for_comparison(build)
        curr_deps = self.resolve_dependencies(build.package, br)
        self.store_deps(build.repo_id, build.package_id, curr_deps)
        if curr_deps is not None:
            build.deps_resolved = True
        if prev:
            prev_deps = self.get_deps_from_db(prev.package_id,
                                              prev.repo_id)
            if prev_deps and curr_deps:
                changes = self.create_dependency_changes(prev_deps, curr_deps,
                                                         build_id=build.id,
                                                         prev_build_id=prev.id)
                if changes:
                    self.db.execute(AppliedChange.__table__.insert(), changes)
            keep_builds = util.config['dependency']['keep_build_deps_for']
            boundary_build = self.db.query(Build)\
                                 .filter_by(package_id=build.package_id)\
                                 .order_by(Build.id.desc())\
                                 .offset(keep_builds).first()
            if boundary_build and boundary_build.repo_id:
                self.db.query(Dependency)\
                       .filter_by(package_id=build.package_id)\
                       .filter(Dependency.repo_id <
                               boundary_build.repo_id)\
                       .delete(synchronize_session=False)

    def run(self):
        # pylint: disable=E1101
        unprocessed = self.db.query(Build)\
                             .filter_by(deps_processed=False)\
                             .filter((Build.state.in_(Build.FINISHED_STATES)) |
                                     (Build.repo_id != None))\
                             .options(joinedload(Build.package))\
                             .order_by(Build.repo_id).all()
        # TODO repo_id
        self.group = util.get_build_group(self.koji_session)

        for repo_id, builds in itertools.groupby(unprocessed,
                                                 lambda build: build.repo_id):
            builds = list(builds)
            self.prepare_sack(repo_id)
            if self.sack:
                brs = util.get_rpm_requires(self.koji_session,
                                            [dict(name=b.package.name,
                                                  version=b.version,
                                                  release=b.release,
                                                  arch='src') for b in builds])
                for build, br in zip(builds, brs):
                    if build.repo_id:
                        self.process_build(build, br)
            self.db.query(Build).filter(Build.id.in_([b.id for b in builds]))\
                                .update({'deps_processed': True},
                                        synchronize_session=False)
            self.db.commit()

class Resolver(KojiService):

    def __init__(self, log=None, db=None, koji_session=None,
                 repo_cache=None, backend=None):
        super(Resolver, self).__init__(log=log, db=db,
                                       koji_session=koji_session)
        self.repo_cache = repo_cache or RepoCache(self.koji_session)
        self.backend = backend or Backend(db=self.db,
                                          koji_session=self.koji_session,
                                          log=self.log)

    def get_handled_exceptions(self):
        return ([librepo.LibrepoException] +
                super(Resolver, self).get_handled_exceptions())

    def create_task(self, cls):
        return cls(log=self.log, db=self.db, koji_session=self.koji_session,
                   repo_cache=self.repo_cache, backend=self.backend)

    def process_repo_generation_requests(self):
        latest_request = self.db.query(RepoGenerationRequest)\
                                .order_by(RepoGenerationRequest.repo_id
                                          .desc())\
                                .first()
        if latest_request:
            repo_id = latest_request.repo_id
            [last_repo] = (self.db.query(Repo.repo_id)
                           .order_by(Repo.repo_id.desc())
                           .first() or [0])
            if repo_id > last_repo:
                self.create_task(GenerateRepoTask).run(repo_id)
            self.db.query(RepoGenerationRequest)\
                   .filter(RepoGenerationRequest.repo_id <= repo_id)\
                   .delete()
            self.db.commit()

    def main(self):
        self.create_task(ProcessBuildsTask).run()
        self.process_repo_generation_requests()
