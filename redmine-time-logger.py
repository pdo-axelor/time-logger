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


Allocation = namedtuple('Allocation', ['issue', 'comment', 'hours'])


class TimeLogger:
    config_path = '~/.time-logger.json'

    def __init__(self, log_date, daily_hours):
        self.log_date = log_date
        self.daily_hours = daily_hours
        self.remaining_hours = self.daily_hours
        self.open_redmine()

    def open_redmine(self):
        config_path = os.path.expanduser(self.config_path)

        if os.path.exists(config_path):
            with open(config_path) as file:
                config = json.load(file)
        else:
            config = {}
        original_config_dump = json.dumps(config, sort_keys=True)

        key = config.get('key')
        if key:
            key = key.encode()
        else:
            key = Fernet.generate_key()
            config['key'] = key.decode()
        fernet = Fernet(key)

        redmine_config = config.get('redmine')
        if not redmine_config:
            redmine_config = {}
            config['redmine'] = redmine_config

        url = redmine_config.get('url')
        username_token = redmine_config.get('username')
        password_token = redmine_config.get('password')

        if not url:
            url = input(f'Redmine URL: ')
            redmine_config['url'] = url

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

        self.redmine = Redmine(url, username=username, password=password)
        self.current_user = self.redmine.user.get('current')

        if json.dumps(config, sort_keys=True) != original_config_dump:
            with open(config_path, 'w') as file:
                json.dump(config, file, indent=2)

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
            print(f'{issue_index}) {self.format_issue(issue)}', end='')

            default_comment = DEFAULT_COMMENTS.get(
                issue.tracker.name, DEFAULT_COMMENTS[None])

            if issue_index < len(to_allocate_issues):
                default_hours = self.compute_hours_per_issue(
                    self.remaining_hours, len(to_allocate_issues) - len(allocations))
            else:
                default_hours = round(self.remaining_hours, 2)

            comment_and_hours = input(' | ' +
                                      _(f'comment and hours (default: {default_comment} {default_hours}): '))
            comment, hours = self.parse_comment_and_hours(
                comment_and_hours, default_comment, default_hours)

            self.remaining_hours -= hours

            if hours:
                allocations.append(Allocation(issue, comment, hours))
        print()

    @classmethod
    def parse_comment_and_hours(cls, comment_and_hours, default_comment, default_hours):
        if not comment_and_hours:
            comment = default_comment
            hours = default_hours
        else:
            try:
                comment, hours = comment_and_hours.rsplit(' ', 1)
                hours = float(hours)
            except ValueError:
                try:
                    hours = float(comment_and_hours)
                    comment = default_comment
                except ValueError:
                    comment = comment_and_hours
                    hours = default_hours
        return comment, hours

    def run_to_allocate_issues(self, allocations, to_allocate_issues):
        print(
            _(f'Remaining {self.remaining_hours} hours to allocate on {len(to_allocate_issues)} issue updated by you on {self.log_date}:',
                f'Remaining {self.remaining_hours} hours to allocate on {len(to_allocate_issues)} issues updated by you on {self.log_date}:',
                len(to_allocate_issues)))
        for issue in to_allocate_issues:
            print(f'{self.format_issue(issue)}')
        print()
        self.allocate(allocations, to_allocate_issues)

    def run(self):
        worked_issue_ids = set()
        existing_time_entries = set()
        for time_entry in self.redmine.time_entry.filter(spent_on=self.log_date, user_id='me', sort='spent_on:desc'):
            self.remaining_hours -= time_entry.hours
            worked_issue_ids.add(time_entry.issue.id)
            existing_time_entries.add(time_entry)

        print(
            _(f'Hours already logged: {self.daily_hours - self.remaining_hours}'))
        for time_entry in existing_time_entries:
            issue = self.redmine.issue.get(time_entry.issue.id)
            print(
                f'{self.format_issue(issue)}: {time_entry.comments} {time_entry.hours}')
        print()

        if self.remaining_hours == 0:
            print(_(f'All done!'))
            return

        if self.remaining_hours < 0:
            print(_(f'Negative remaining hours: {self.remaining_hours}'))
            return -1

        to_allocate_issues = []
        for issue in (e for e in self.redmine.issue.filter(limit=50, updated_on=self.log_date, updated_by='me', status_id='*', sort='id')
                      if e.id not in worked_issue_ids):
            if self.commented_by_current_user(issue):
                to_allocate_issues.append(issue)
            else:
                print(_(f'Not updated by you: {self.format_issue(issue)}'))

        allocations = []

        if to_allocate_issues:
            self.run_to_allocate_issues(allocations, to_allocate_issues)
        else:
            print(_(f'Found no issues updated by you on {self.log_date}'))
            print()

        if self.remaining_hours > 0:
            self.run_suggested_additional_issues(
                allocations, to_allocate_issues)

        if not allocations:
            print(_(f'Nothing to allocate'))
            return

        on_ticket = next(e for e in self.redmine.enumeration.filter(
            resource='time_entry_activities') if e.name == 'On ticket')

        print(_(f'Time log to create:', 'Time logs to create:', len(allocations)))
        for allocation in allocations:
            print(
                f'{self.format_issue(allocation.issue)}: {allocation.comment} {allocation.hours}')
        confirm = input(_(f'Confirm? (y/N): ')).lower() == 'y'

        if not confirm:
            return

        for allocation in allocations:
            time_entry = self.redmine.time_entry.create(
                issue_id=allocation.issue.id,
                spent_on=self.log_date,
                hours=f'{allocation.hours:.2f}',
                comments=allocation.comment,
                activity_id=on_ticket.id
            )

        print('Done')

    def run_suggested_additional_issues(self, allocations, to_allocate_issues):
        if to_allocate_issues:
            print(_(f'Hours still remaining: {self.remaining_hours}'))

        suggested_additional_issues = []
        print(_(f'Suggested recently updated open issues assigned to you:'))
        for issue in self.redmine.issue.filter(limit=20, assigned_to_id='me', status_id='open', sort='updated_on:desc'):
            if any(issue.id == allocation.issue.id for allocation in allocations):
                continue
            print(f'{self.format_issue(issue)}')
            suggested_additional_issues.append(issue)
            if len(suggested_additional_issues) >= 10:
                break
        print()

        default_additional_issues = suggested_additional_issues[:5]
        default_additional_issue_ids = ' '.join(
            str(issue.id) for issue in default_additional_issues)
        additional_issue_ids = [int(e) for e in input(
            _(f'Additional issue IDs (default: {default_additional_issue_ids}): ')).split()]
        if additional_issue_ids:
            additional_issues = [self.redmine.issue.get(
                id) for id in additional_issue_ids]
        else:
            additional_issues = default_additional_issues

        self.allocate(allocations, additional_issues)


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--log-date', help='log date', default='today')
    parser.add_argument('--daily-hours', help='daily hours',
                        default=DEFAULT_DAILY_HOURS)
    options = parser.parse_args()

    if options.log_date == 'today':
        options.log_date = datetime.date.today()
    elif isinstance(options.log_date, str):
        options.log_date = datetime.date.fromisoformat(options.log_date)

    time_logger = TimeLogger(options.log_date, options.daily_hours)

    try:
        return time_logger.run()
    except KeyboardInterrupt:
        print()


if __name__ == '__main__':
    sys.exit(main())