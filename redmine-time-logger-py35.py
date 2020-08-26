#!/usr/bin/env python3
"""Redmine time logger

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.
"""

from collections import namedtuple
from cryptography.fernet import Fernet
from redminelib import Redmine
import argparse
import datetime
import getpass
import gettext
import itertools
import json
import os
import redminelib.exceptions
import sys

DEFAULT_DAILY_HOURS = 7.8

DEFAULT_COMMENTS = {
    None: 'WIP',
    'Anomaly': 'Fix',
    'Feature': 'Implement',
    'Support': 'Feedback',
    'Proposal': 'Feedback',
    'Anomalie': 'Fix',
    'Evolution': 'Implement',
    'Fonctionnalité prévue': 'Implement',
    'Intervention/assistance': 'Feedback',
    'Pack': '',
    'Project': 'On project',
}


def _(singular, plural=None, n=0):
    return (gettext.gettext(singular) if plural is None
            else gettext.ngettext(singular, plural, n))


Allocation = namedtuple(
    'Allocation', ['issue', 'project', 'hours', 'comment', 'activity'])


class TimeLogger:
    config_path = os.path.expanduser('~/.time-logger.json')

    def __init__(self, options=None):
        if options:
            log_date = options.log_date
            daily_hours = options.daily_hours
            ignored_statuses = options.ignored_statuses
        else:
            log_date = datetime.date.today()
            daily_hours = DEFAULT_DAILY_HOURS
            ignored_statuses = None

        self.read_config()
        self.log_date = log_date
        self.configure_daily_hours(daily_hours)
        self.remaining_hours = self.daily_hours
        self.open_redmine()
        self.configure_ignored_statuses(ignored_statuses)
        self.write_config()

    def read_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path) as file:
                self.config = json.load(file)
        else:
            self.config = {}
        self.original_config_dump = json.dumps(self.config, sort_keys=True)

    def write_config(self):
        config_dump = json.dumps(self.config, sort_keys=True)
        if self.original_config_dump != config_dump:
            with open(self.config_path, 'w') as file:
                json.dump(self.config, file, indent=2)

    def configure_daily_hours(self, daily_hours):
        if daily_hours:
            self.daily_hours = daily_hours
        else:
            self.daily_hours = self.config.get(
                'dailyHours') or DEFAULT_DAILY_HOURS
        self.config['dailyHours'] = self.daily_hours

    def configure_ignored_statuses(self, ignored_status_names):
        if ignored_status_names is None:
            ignored_status_names = self.config.get('ignoredStatuses')
        self.config['ignoredStatuses'] = ignored_status_names
        self.ignored_status_ids = (set(
            e.id for e in self.redmine.issue_status.all() if e.name in ignored_status_names)
            if ignored_status_names else set())

    def open_redmine(self):
        key = self.config.get('key')
        if key:
            key = key.encode()
        else:
            key = Fernet.generate_key()
            self.config['key'] = key.decode()
        fernet = Fernet(key)

        redmine_config = self.config.get('redmine')
        if not redmine_config:
            redmine_config = {}
            self.config['redmine'] = redmine_config

        url = redmine_config.get('url')
        username_token = redmine_config.get('username')
        password_token = redmine_config.get('password')

        if not url:
            url = input(_('Redmine URL: '))
            if ':' not in url:
                url = 'https://' + url
            redmine_config['url'] = url

        while True:
            try:
                if username_token:
                    username = fernet.decrypt(username_token.encode()).decode()
                else:
                    username = input(_('Username: '))
                    redmine_config['username'] = fernet.encrypt(
                        username.encode()).decode()

                if password_token:
                    password = fernet.decrypt(password_token.encode()).decode()
                else:
                    password = getpass.getpass(_('Password: '))
                    redmine_config['password'] = fernet.encrypt(
                        password.encode()).decode()

                self.redmine = Redmine(
                    url, username=username, password=password)
                self.current_user = self.redmine.user.get('current')
                break
            except redminelib.exceptions.AuthError as e:
                print(str(e), file=sys.stderr)
                username_token = None
                password_token = None

        self.activities = list(self.redmine.enumeration.filter(
            resource='time_entry_activities'))

        activity_name = redmine_config.get('activity')
        if activity_name:
            self.default_activity = next(
                e for e in self.activities if e.name == activity_name)
        else:
            print(_('Activities:'))
            for activity in self.activities:
                print('{}: {}'.format((activity.id), (activity.name)))
            activity_id = int(input(_('Default activity ID for issues: ')))
            self.default_activity = next(
                e for e in self.activities if e.id == activity_id)
            redmine_config['activity'] = self.default_activity.name

    @classmethod
    def compute_hours_per_issue(cls, hours, issue_count):
        return round(round((hours / issue_count) / .05) * .05, 2)

    @classmethod
    def format_issue(cls, issue):
        return '{} - {} #{}: {}'.format((issue.project.name), (issue.tracker.name), (issue.id), (issue.subject))

    def commented_by_current_user(self, issue):
        return any(journal.created_on.date() == self.log_date
                   and journal.user.id == self.current_user.id for journal in issue.journals)

    def allocate_issues(self, allocations, to_allocate_issues):
        for issue_index, issue in zip(itertools.count(1), to_allocate_issues):
            self.processed_issue_ids.add(issue.id)
            print('{}) {}'.format((issue_index), (self.format_issue(issue))), end='')

            default_comment = DEFAULT_COMMENTS.get(
                issue.tracker.name, DEFAULT_COMMENTS[None])

            if issue_index < len(to_allocate_issues):
                default_hours = self.compute_hours_per_issue(
                    self.remaining_hours, len(to_allocate_issues) - issue_index + 1)
            else:
                default_hours = round(self.remaining_hours, 2)

            hours_and_comment = input(' | ' +
                                      _('hours comment '
                                        '(default: {} {}): '.format((default_hours), (default_comment))))
            hours, comment = self.parse_hours_and_comment(
                hours_and_comment, default_hours, default_comment)

            self.remaining_hours -= hours

            if hours:
                allocations.append(Allocation(
                    issue, None, hours, comment, self.default_activity))
        print()

    def allocate_projects(self, allocations, to_allocate_projects):
        try:
            default_activity = next(
                activity for activity in self.activities if 'project' in activity.name.lower())
        except StopIteration:
            default_activity = self.activities[0] if self.activities else None

        for project_index, project in zip(itertools.count(1), to_allocate_projects):
            print('{}'.format((project.name)), end='')

            default_comment = DEFAULT_COMMENTS.get(
                'Project', DEFAULT_COMMENTS[None])

            if project_index < len(to_allocate_projects):
                default_hours = self.compute_hours_per_issue(
                    self.remaining_hours, len(to_allocate_projects) - project_index + 1)
            else:
                default_hours = round(self.remaining_hours, 2)

            hours_comment_and_activity = input(' | ' +
                                               _('hours comment activity_id '
                                                 '(default: {} {} '
                                                 '{}): '.format((default_hours), (default_comment), (default_activity.id))))
            hours, comment, activity = self.parse_hours_comment_and_activity(
                hours_comment_and_activity, default_hours, default_comment, default_activity)

            self.remaining_hours -= hours

            if hours:
                allocations.append(Allocation(
                    None, project, hours, comment, activity))
        print()

    @classmethod
    def parse_hours_and_comment(cls, text, default_hours, default_comment):
        if not text:
            hours = default_hours
            comment = default_comment
        else:
            try:
                hours, comment = text.split(' ', 1)
                hours = float(hours)
            except ValueError:
                try:
                    hours = float(text)
                    comment = default_comment
                except ValueError:
                    hours = default_hours
                    comment = text
        return hours, comment

    def parse_hours_comment_and_activity(self,
                                         text, default_hours, default_comment, default_activity):
        if not text:
            hours = default_hours
            comment = default_comment
            activity = default_activity
        else:
            try:
                hours, comment = text.split(' ', 1)
                hours = float(hours)
                new_comment, activity_id = comment.rsplit(' ', 1)
                activity_id = int(activity_id)
                activity = next(
                    activity for activity in self.activities if activity.id == activity_id)
                comment = new_comment
            except (ValueError, StopIteration):
                try:
                    hours = float(text)
                    comment = default_comment
                    activity = default_activity
                except ValueError:
                    hours = default_hours
                    comment = text
                    activity = default_activity

        return hours, comment, activity

    def run_to_allocate_issues(self, allocations, to_allocate_issues):
        print(
            _('Remaining {:.2f} hours to allocate '
              'on {} issue ({}) updated by you:'.format((self.remaining_hours), (len(to_allocate_issues)), (self.log_date)),
                'Remaining {:.2f} hours to allocate '
                'on {} issues ({}) updated by you:'.format((self.remaining_hours), (len(to_allocate_issues)), (self.log_date)),
                len(to_allocate_issues)))
        for issue in to_allocate_issues:
            print(
                '{}'.format((self.format_issue(issue))))
        print()
        self.allocate_issues(allocations, to_allocate_issues)

    @classmethod
    def get_issue(cls, time_entry):
        try:
            return time_entry.issue
        except redminelib.exceptions.ResourceAttrError:
            return None

    def run(self):
        self.processed_issue_ids = set()
        existing_time_entries = set()
        for time_entry in self.redmine.time_entry.filter(spent_on=self.log_date, user_id='me',
                                                         sort='spent_on:desc'):
            self.remaining_hours -= time_entry.hours
            issue = self.get_issue(time_entry)
            if issue:
                self.processed_issue_ids.add(issue.id)
            existing_time_entries.add(time_entry)

        print(
            _('Hours already logged: {:.2f}'.format((self.daily_hours - self.remaining_hours))))
        for time_entry in existing_time_entries:
            issue = self.get_issue(time_entry)
            if issue:
                issue = self.redmine.issue.get(time_entry.issue.id)
                print(
                    '{}: {} {}'.format((self.format_issue(issue)), (time_entry.hours), (time_entry.comments)))
            else:
                print(
                    '{}: {} {}'.format((time_entry.project), (time_entry.hours), (time_entry.comments)))
        print()

        if round(self.remaining_hours, 2) == 0:
            print(_('All already done!'))
            return

        if self.remaining_hours < 0:
            print(_('Negative remaining hours: {:.2f}'.format((self.remaining_hours))))
            return -1

        to_allocate_issues = []
        for issue in (e for e in self.redmine.issue.filter(limit=50, updated_on=self.log_date,
                                                           updated_by='me', status_id='*',
                                                           sort='id')
                      if e.id not in self.processed_issue_ids):
            if self.commented_by_current_user(issue):
                to_allocate_issues.append(issue)

        for issue in (e for e in self.redmine.issue.filter(limit=50, created_on=self.log_date,
                                                           author_id='me', status_id='*',
                                                           sort='id')
                      if e.id not in self.processed_issue_ids):
            if not any(issue.id == e.id for e in to_allocate_issues):
                to_allocate_issues.append(issue)

        allocations = []

        if to_allocate_issues:
            self.run_to_allocate_issues(allocations, to_allocate_issues)
        else:
            print(
                _('Found no more issues updated/created by you on {}'.format((self.log_date))))
            print()

        if self.remaining_hours > 0:
            print(_('Hours still remaining: {:.2f}'.format((self.remaining_hours))))
            search_suggested = input(
                _('Search for more issues? (Y/n): ')).lower() != 'n'
            if search_suggested:
                self.run_suggested_additional_issues(allocations)

        if self.remaining_hours > 0 and input(_('Log time on projects? (Y/n): ')).lower() != 'n':
            self.run_log_on_projects(allocations)

        if not allocations:
            print(_('Nothing to allocate'))
            return

        print(_('Time log to create:', 'Time logs to create:', len(allocations)))
        for allocation in allocations:
            if allocation.issue:
                print(
                    '{} | '.format((self.format_issue(allocation.issue)))
                    + _('hours: {:.2f}, comment: {!r}, '
                        'activity: {!r}'.format((allocation.hours), (allocation.comment), (allocation.activity.name))))
            elif allocation.project:
                print(
                    '{} | '.format((allocation.project.name))
                    + _('hours: {:.2f}, comment: {!r}, '
                        'activity: {!r}'.format((allocation.hours), (allocation.comment), (allocation.activity.name))))
        confirm = input(_('Confirm? (y/N): ')).lower() == 'y'

        if not confirm:
            return

        for allocation in allocations:
            if allocation.issue:
                time_entry = self.redmine.time_entry.create(
                    issue_id=allocation.issue.id,
                    spent_on=self.log_date,
                    hours='{:.2f}'.format((allocation.hours)),
                    comments=allocation.comment,
                    activity_id=allocation.activity.id
                )
            elif allocation.project:
                time_entry = self.redmine.time_entry.create(
                    project_id=allocation.project.id,
                    spent_on=self.log_date,
                    hours='{:.2f}'.format((allocation.hours)),
                    comments=allocation.comment,
                    activity_id=allocation.activity.id
                )

        print(_('Done'))

    def run_log_on_projects(self, allocations):
        projects = []
        print(_('Projects:'))
        for project in self.redmine.project.all():
            projects.append(project)
            print('{}: {}'.format((project.id), (project.name)))
        print()

        print(_('Activities:'))
        for activity in self.activities:
            print('{}: {}'.format((activity.id), (activity.name)))
        print()

        project_ids = [int(e) for e in input(_('Project IDs: ')).split()]
        if not project_ids:
            return
        to_allocate_projects = [
            project for project in projects if project.id in project_ids]
        self.allocate_projects(allocations, to_allocate_projects)

    def run_suggested_additional_issues(self, allocations):
        suggested_additional_issues = []
        show_title = True

        for issue in self.redmine.issue.filter(limit=50, updated_on=self.log_date, updated_by='me',
                                               status_id='open', sort='updated_on:desc'):
            if issue.status.id in self.ignored_status_ids\
                    or issue.id in self.processed_issue_ids\
                    or self.commented_by_current_user(issue):
                continue
            if show_title:
                print(_('Recently updated open issues watched by you:'))
                show_title = False
            print('{}'.format((self.format_issue(issue))))
            suggested_additional_issues.append(issue)
            if len(suggested_additional_issues) >= 10:
                break

        if len(suggested_additional_issues) < 10:
            for issue in self.redmine.issue.filter(limit=50, assigned_to_id='me', status_id='open',
                                                   sort='updated_on:desc'):
                if issue.status.id in self.ignored_status_ids\
                        or issue.id in self.processed_issue_ids\
                        or any(issue.id == e.id for e in suggested_additional_issues):
                    continue
                if show_title:
                    print(_('Recently updated open issues assigned to you:'))
                    show_title = False
                print('{}'.format((self.format_issue(issue))))
                suggested_additional_issues.append(issue)
                if len(suggested_additional_issues) >= 10:
                    break

        print()

        suggested_additional_issue_map = {
            e.id: e for e in suggested_additional_issues}
        default_additional_issues = suggested_additional_issues[:5]
        default_additional_issue_ids = ' '.join(
            str(issue.id) for issue in default_additional_issues)
        additional_issue_ids = [int(e) for e in input(
            _('Additional issue IDs (default: {}): '.format((default_additional_issue_ids)))).split()]
        if additional_issue_ids:
            additional_issues = []
            for id in additional_issue_ids:
                if not id:
                    break
                if id in suggested_additional_issue_map:
                    issue = suggested_additional_issue_map[id]
                else:
                    issue = self.redmine.issue.get(id)
                additional_issues.append(issue)
        else:
            additional_issues = default_additional_issues

        self.allocate_issues(allocations, additional_issues)


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--log-date', help='log date', default='today')
    parser.add_argument('--daily-hours', help='daily hours',
                        default=None, type=float)
    parser.add_argument('--ignored-statuses',
                        help='ignored status names', type=str, nargs='*')
    options = parser.parse_args()

    if options.log_date == 'today':
        options.log_date = datetime.date.today()
    elif isinstance(options.log_date, str):
        options.log_date = datetime.datetime.strptime(
            options.log_date, '%Y-%m-%d').date()

    time_logger = TimeLogger(options)

    try:
        return time_logger.run()
    except KeyboardInterrupt:
        print()


if __name__ == '__main__':
    sys.exit(main())
