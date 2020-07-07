#!/usr/bin/env python3

from collections import namedtuple
from cryptography.fernet import Fernet
from pprint import pprint
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
    None: 'wip',
    'Anomaly': 'fix',
    'Feature': 'implement',
    'Support': 'feedback',
    'Proposal': 'feedback',
}


def _(singular, plural=None, n=0):
    if plural is None:
        return gettext.gettext(singular)
    return gettext.ngettext(singular, plural, n)


Allocation = namedtuple('Allocation', ['issue', 'hours', 'comment'])


class TimeLogger:
    config_path = os.path.expanduser('~/.time-logger.json')

    def __init__(self, log_date, daily_hours):
        self.read_config()
        self.log_date = log_date
        self.configure_daily_hours(daily_hours)
        self.remaining_hours = self.daily_hours
        self.open_redmine()
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
            url = input(f'Redmine URL: ')
            if ':' not in url:
                url = 'https://' + url
            redmine_config['url'] = url

        while True:
            try:
                if username_token:
                    username = fernet.decrypt(username_token.encode()).decode()
                else:
                    username = input(f'Username: ')
                    redmine_config['username'] = fernet.encrypt(
                        username.encode()).decode()

                if password_token:
                    password = fernet.decrypt(password_token.encode()).decode()
                else:
                    password = getpass.getpass(f'Password: ')
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

        activities = list(self.redmine.enumeration.filter(
            resource='time_entry_activities'))

        activity_name = redmine_config.get('activity')
        if activity_name:
            self.default_activity = next(
                e for e in activities if e.name == activity_name)
        else:
            print('Activities:')
            for activity in activities:
                print(f'#{activity.id}: {activity.name}')
            activity_id = int(input('Default activity ID: '))
            self.default_activity = next(
                e for e in activities if e.id == activity_id)
            redmine_config['activity'] = self.default_activity.name

    @classmethod
    def compute_hours_per_issue(cls, hours, issue_count):
        return round(round((hours / issue_count) / .05) * .05, 2)

    @classmethod
    def format_issue(cls, issue):
        return f'{issue.project.name} - {issue.tracker.name} #{issue.id}: {issue.subject}'

    def commented_by_current_user(self, issue):
        return any(journal.created_on.date() == self.log_date and journal.user.id == self.current_user.id for journal in issue.journals)

    def allocate(self, allocations, to_allocate_issues):
        for issue_index, issue in zip(itertools.count(1), to_allocate_issues):
            self.processed_issue_ids.add(issue.id)
            print(f'{issue_index}) {self.format_issue(issue)}', end='')

            default_comment = DEFAULT_COMMENTS.get(
                issue.tracker.name, DEFAULT_COMMENTS[None])

            if issue_index < len(to_allocate_issues):
                default_hours = self.compute_hours_per_issue(
                    self.remaining_hours, len(to_allocate_issues) - issue_index + 1)
            else:
                default_hours = round(self.remaining_hours, 2)

            hours_and_comment = input(' | ' +
                                      _(f'hours and comments (default: {default_hours} {default_comment}): '))
            hours, comment = self.parse_hours_and_comment(
                hours_and_comment, default_hours, default_comment)

            self.remaining_hours -= hours

            if hours:
                allocations.append(Allocation(issue, hours, comment))
        print()

    @classmethod
    def parse_hours_and_comment(cls, hours_and_comment, default_hours, default_comment):
        if not hours_and_comment:
            hours = default_hours
            comment = default_comment
        else:
            try:
                hours, comment = hours_and_comment.split(' ', 1)
                hours = float(hours)
            except ValueError:
                try:
                    hours = float(hours_and_comment)
                    comment = default_comment
                except ValueError:
                    hours = default_hours
                    comment = hours_and_comment
        return hours, comment

    def run_to_allocate_issues(self, allocations, to_allocate_issues):
        print(
            _(f'Remaining {self.remaining_hours} hours to allocate on {len(to_allocate_issues)} issue ({self.log_date}) updated by you:',
                f'Remaining {self.remaining_hours} hours to allocate on {len(to_allocate_issues)} issues ({self.log_date}) updated by you:',
                len(to_allocate_issues)))
        for issue in to_allocate_issues:
            print(
                f'{self.format_issue(issue)}')
        print()
        self.allocate(allocations, to_allocate_issues)

    @classmethod
    def get_issue(cls, time_entry):
        try:
            return time_entry.issue
        except redminelib.exceptions.ResourceAttrError:
            return None

    def run(self):
        self.processed_issue_ids = set()
        worked_issue_ids = set()
        existing_time_entries = set()
        for time_entry in self.redmine.time_entry.filter(spent_on=self.log_date, user_id='me', sort='spent_on:desc'):
            self.remaining_hours -= time_entry.hours
            issue = self.get_issue(time_entry)
            if issue:
                worked_issue_ids.add(issue.id)
            existing_time_entries.add(time_entry)

        print(
            _(f'Hours already logged: {self.daily_hours - self.remaining_hours:.2f}'))
        for time_entry in existing_time_entries:
            issue = self.get_issue(time_entry)
            if issue:
                issue = self.redmine.issue.get(time_entry.issue.id)
                print(
                    f'{self.format_issue(issue)}: {time_entry.comments} {time_entry.hours}')
            else:
                print(
                    f'{time_entry.project}: {time_entry.comments} {time_entry.hours}')
        print()

        if round(self.remaining_hours, 2) == 0:
            print(_(f'All already done!'))
            return

        if self.remaining_hours < 0:
            print(_(f'Negative remaining hours: {self.remaining_hours}'))
            return -1

        to_allocate_issues = []
        for issue in (e for e in self.redmine.issue.filter(limit=50, updated_on=self.log_date, updated_by='me', status_id='*', sort='id')
                      if e.id not in worked_issue_ids):
            if self.commented_by_current_user(issue):
                to_allocate_issues.append(issue)

        allocations = []

        if to_allocate_issues:
            self.run_to_allocate_issues(allocations, to_allocate_issues)
        else:
            print(_(f'Found no more issues updated by you on {self.log_date}'))
            print()

        if self.remaining_hours > 0:
            if to_allocate_issues:
                print(_(f'Hours still remaining: {self.remaining_hours}'))
            search_suggested = input(
                _(f'Search for more issues? (Y/n): ')).lower() != 'n'
            if search_suggested:
                self.run_suggested_additional_issues(allocations)

        if not allocations:
            print(_(f'Nothing to allocate'))
            return

        print(_(f'Time log to create:', 'Time logs to create:', len(allocations)))
        for allocation in allocations:
            print(
                f'{self.format_issue(allocation.issue)}: {allocation.hours:.2f} {allocation.comment}')
        confirm = input(_(f'Confirm? (y/N): ')).lower() == 'y'

        if not confirm:
            return

        for allocation in allocations:
            time_entry = self.redmine.time_entry.create(
                issue_id=allocation.issue.id,
                spent_on=self.log_date,
                hours=f'{allocation.hours:.2f}',
                comments=allocation.comment,
                activity_id=self.default_activity.id
            )

        print('Done')

    def run_suggested_additional_issues(self, allocations):
        suggested_additional_issues = []

        print(_(f'Recently updated open issues watched by you:'))
        for issue in self.redmine.issue.filter(limit=50, updated_on=self.log_date, updated_by='me', status_id='open', sort='updated_on:desc'):
            if len(suggested_additional_issues) >= 10:
                break
            if issue.id in self.processed_issue_ids or self.commented_by_current_user(issue):
                continue
            print(f'{self.format_issue(issue)}')
            suggested_additional_issues.append(issue)

        print(_(f'Recently updated open issues assigned to you:'))
        for issue in self.redmine.issue.filter(limit=20, assigned_to_id='me', status_id='open', sort='updated_on:desc'):
            if len(suggested_additional_issues) >= 10:
                break
            if issue.id in self.processed_issue_ids or any(issue.id == e.id for e in suggested_additional_issues):
                continue
            print(f'{self.format_issue(issue)}')
            suggested_additional_issues.append(issue)

        print()

        suggested_additional_issue_map = {
            e.id: e for e in suggested_additional_issues}
        default_additional_issues = suggested_additional_issues[:5]
        default_additional_issue_ids = ' '.join(
            str(issue.id) for issue in default_additional_issues)
        additional_issue_ids = [int(e) for e in input(
            _(f'Additional issue IDs (default: {default_additional_issue_ids}): ')).split()]
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

        self.allocate(allocations, additional_issues)


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--log-date', help='log date', default='today')
    parser.add_argument('--daily-hours', help='daily hours',
                        default=None, type=float)
    options = parser.parse_args()

    if options.log_date == 'today':
        options.log_date = datetime.date.today()
    elif isinstance(options.log_date, str):
        options.log_date = datetime.datetime.strptime(
            options.log_date, '%Y-%m-%d').date()

    time_logger = TimeLogger(options.log_date, options.daily_hours)

    try:
        return time_logger.run()
    except KeyboardInterrupt:
        print()


if __name__ == '__main__':
    sys.exit(main())
