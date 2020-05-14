import json
import functools
import urllib
from collections import OrderedDict
from os import path
from copy import deepcopy
from typing import List, Union
from mitmproxy import ctx, flow
from mitmproxy.http import HTTPRequest
from mitmproxy.script import concurrent
from time import ctime
from dateutil.parser import parse


def record_concurrently(replaying: bool = False):
    '''
    A decorator to return a decorator that just executes the function it decorates normally if 'replaying' is true,
    (AKA mitmdump is executing in server-replay mode or reading in a mock file and cleaning it and saving the cleaned
    mock to a new file), otherwise pass the 'concurrant' decorater so that when mitmdump is executing in recording
    mode, that requests will be processed concurrently and not be blocking which can cause proxy errors during
    recording if multiple requests are made in a short timespan.

    Arguments:
        replaying (bool): True if timestamp replacer script is running in server playback mode or cleaning a mock file

    Returns:
        (function): decorator
    '''
    print(f'replaying={replaying}')
    if replaying:
        def nonconcurrent_decorator(func):
            @functools.wraps(func)
            def passthrough_wrapper(*args, **kwargs):
                value = func(*args, **kwargs)
                return value
            return passthrough_wrapper
        return nonconcurrent_decorator
    else:
        return concurrent


class TimestampReplacer:
    def __init__(self):
        self.count = 0
        self.constant = 'constant_value'
        self.json_keys = set()
        self.form_keys = set()
        self.query_keys = set()
        self.bad_keys_filepath = ''
        self.detect_timestamps = False

    def load(self, loader):
        loader.add_option(
            name='keys_to_replace',
            typespec=str,
            default='',
            help='''
            The keys inside a Posted Request's body whose value is a timestamp and needs to be replaced.
            Nested keys whould be written in dot notation.
            '''
        )
        loader.add_option(
            name='detect_timestamps',
            typespec=bool,
            default=False,
            help='''
            Set to True only if recording a mock file. Used to determine which keys need to be replaced in incoming
            request bodies during a mock playback.
            '''
        )
        loader.add_option(
            name='keys_filepath',
            typespec=str,
            default='problematic_keys.json',  # disable-secrets-detection
            help='''
            The path to the file that contains the problematic keys for the test playbook recording that resides
            in the same directory.
            '''
        )
        loader.add_option(
            name='debug',
            typespec=bool,
            default=False,
            help='''
            Set to True to print out additional information for each request that comes in.
            '''
        )

    def running(self):
        # # need to do this because arguments for these options are interpreted as 1 list item
        # query_keys = ctx.options.server_replay_ignore_params
        # ctx.options.server_replay_ignore_params = query_keys[0].split() if len(query_keys) == 1 else query_keys
        # form_keys = ctx.options.server_replay_ignore_payload_params
        # ctx.options.server_replay_ignore_payload_params = form_keys[0].split() if len(form_keys) == 1 else form_keys
        if ctx.options.debug:
            print(f'ctx.options={ctx.options}')

        self.bad_keys_filepath = ctx.options.keys_filepath
        if ctx.options.detect_timestamps:
            print('Detecting Timestamp Fields')
            self.detect_timestamps = True
        else:
            self.load_problematic_keys()

    def _debug_request(self, flow: flow.Flow) -> None:
        '''Print details of the request'''
        req = flow.request
        print(f'{req.method} {req.pretty_url}')
        _, _, path, _, query, _ = urllib.parse.urlparse(req.url)
        queriesArray = urllib.parse.parse_qsl(query, keep_blank_values=True)
        print(f'queriesArray={queriesArray}')
        if req.multipart_form:
            print(f'multipart_form data = {req.multipart_form.items()}')
        if req.urlencoded_form:
            print(f'urlencoded_form data = {req.urlencoded_form.items()}')
        print(f'hashed_data={ServerPlayback._hash(self, flow)}')

    @record_concurrently(
        replaying=bool(
            ctx.options.server_replay or (ctx.options.rfile and ctx.options.save_stream_file)
        )
    )
    def request(self, flow: flow.Flow) -> None:
        self.count += 1
        if ctx.options.debug:
            self._debug_request(flow)
        req = flow.request
        if ctx.options.detect_timestamps:
            self.handle_url_query(flow)
        if req.method == 'POST':
            raw_content = req.raw_content
            if raw_content is not None:
                content = req.raw_content.decode()
            else:
                content = ''
            json_data = content.startswith('{')
            if json_data:
                content = json.loads(content, object_pairs_hook=OrderedDict)

            if ctx.options.detect_timestamps:
                if req.multipart_form:
                    self.handle_multipart_form(flow)
                # form_urlencoded = 'application/x-www-form-urlencoded' in req.headers.get('content-type', '').lower()
                elif req.urlencoded_form:
                    self.handle_urlencoded_form(flow)
                elif json_data:
                    print('req num: {}\n{}'.format(self.count, content))
                    for problem_key in self.determine_problematic_keys(content):
                        self.json_keys.add(problem_key)
            elif json_data:
                self.modify_json_body(flow, content)
        if ctx.options.detect_timestamps:
            print('updating problem_keys file at "{}"'.format(self.bad_keys_filepath))
            self.update_problem_keys_file()

    def modify_json_body(self, flow: flow.Flow, json_body: dict) -> None:
        '''Modify the json body of a request by replacing any timestamp data with the number of the current request.

        Args:
            flow (flow.Flow): The flow whose request body is to be modified.
            json_body (dict): The request body to modify.
        '''
        original_content = deepcopy(json_body)
        body = json_body
        modified = False
        keys_to_replace = ctx.options.keys_to_replace.split() or []
        print('{}'.format(keys_to_replace))
        for key_path in keys_to_replace:
            body = json_body
            keys = key_path.split('.')
            print('keypath parts: {}'.format(keys))
            lastkey = keys[-1]
            print('lastkey: {}'.format(lastkey))
            skip_key = False
            for k in keys[:-1]:
                if k in body:
                    body = body[k]
                    # print('updated body: {}'.format(body))
                elif isinstance(body, list) and k.isdigit():
                    if int(k) > len(body) - 1:
                        skip_key = True
                        break
                    body = body[int(k)]
                else:
                    skip_key = True
                    break
            if not skip_key:
                if lastkey in body:
                    print('modifying request to "{}"'.format(flow.request.pretty_url))
                    # body[lastkey] = self.count
                    body[lastkey] = self.constant
                    modified = True
                elif isinstance(body, list) and lastkey.isdigit() and int(lastkey) <= len(body) - 1:
                    print('modifying request to "{}"'.format(flow.request.pretty_url))
                    # body[int(lastkey)] = self.count
                    body[int(lastkey)] = self.constant
                    modified = True
        if modified:
            print('original request body:\n{}'.format(json.dumps(original_content, indent=4)))
            print('modified request body:\n{}'.format(json.dumps(json_body, indent=4)))
            # flow.request.raw_content = json.dumps(json_body).encode()
            flow.request.set_content(json.dumps(json_body).encode())

    def handle_url_query(self, flow: flow.Flow) -> None:
        query_data = flow.request._get_query()
        print('query_data: {}'.format(query_data))
        for key, val in query_data:
            # don't bother trying to interpret an argument less than 4 characters as some type of timestamp
            if len(val) > 4:
                try:
                    parse(val)
                    self.query_keys.add(key)
                except ValueError:
                    pass

    def handle_multipart_form(self, flow: flow.Flow) -> None:
        '''Used when detecting what keys in a multipart form to ignore.

        Args:
            flow (flow.Flow): The flow whose request is being inspected
        '''
        for key, val in flow.request.multipart_form.items(multi=True):
            # don't bother trying to interpret an argument less than 4 characters as some type of timestamp
            if len(val) > 4:
                try:
                    parse(val)
                    self.form_keys.add(key)
                except ValueError:
                    pass

    def handle_urlencoded_form(self, flow: flow.Flow) -> None:
        '''Used when detecting what keys in an url encoded parameters to ignore.

        Args:
            flow (flow.Flow): The flow whose request is being inspected
        '''
        for key, val in flow.request.urlencoded_form.items(multi=True):
            # don't bother trying to interpret an argument less than 4 characters as some type of timestamp
            if len(val) > 4:
                try:
                    parse(val)
                    self.form_keys.add(key)
                except ValueError:
                    pass

    def determine_problematic_keys(self, content: dict) -> List[str]:
        '''Given a json request body, return the keys (in dot notation) whose values are potentially timestamp data.

        Args:
            content (dict): The json request body to iterate through and find problematic timestamp data.

        Returns:
            List[str]: A list of keys (in dot notation, e.g. 'query.filter.time' is an example of what could be one
                problematic key) whose values are potentially timestamp data.
        '''
        def travel_dict(obj: Union[dict, list], key_path='') -> List[str]:
            bad_key_paths = []
            if isinstance(obj, dict):
                for key, val in obj.items():
                    sub_key_path = '{}.{}'.format(key_path, key) if key_path else key
                    if isinstance(val, (list, dict)):
                        bad_key_paths.extend(travel_dict(val, sub_key_path))
                    else:
                        is_string = isinstance(val, str) and len(val) > 4
                        possible_timestamp = isinstance(val, (int, float)) and len(str(val)) >= 8
                        try:
                            if is_string or possible_timestamp:
                                # print('req num: {} keypath: {}'.format(self.count, sub_key_path))
                                for_eval = val
                                if possible_timestamp:
                                    if isinstance(for_eval, float):
                                        digits = str(val).split('.')
                                        for_eval = digits[0]
                                    if len(str(for_eval)) < 13:
                                        parse(ctime(val))
                                    else:
                                        parse(ctime(val / 1000.0))
                                else:
                                    parse(val)
                                # if it continues to the next line that means it successfully parsed the object
                                # and it's some sort of time-related object
                                bad_key_paths.append(sub_key_path)
                        except (ValueError, OverflowError):
                            pass
            elif isinstance(obj, list):
                for i, val in enumerate(obj):
                    sub_key_path = '{}.{}'.format(key_path, i) if key_path else i
                    if isinstance(val, (list, dict)):
                        bad_key_paths.extend(travel_dict(val, sub_key_path))
                    else:
                        is_string = isinstance(val, str) and len(val) > 4
                        possible_timestamp = isinstance(val, (int, float)) and len(str(val)) >= 8
                        try:
                            if is_string or possible_timestamp:
                                # print('req num: {} keypath: {}'.format(self.count, sub_key_path))
                                for_eval = val
                                if possible_timestamp:
                                    if isinstance(for_eval, float):
                                        digits = str(val).split('.')
                                        for_eval = digits[0]
                                    if len(str(for_eval)) < 13:
                                        parse(ctime(val))
                                    else:
                                        parse(ctime(val / 1000.0))
                                else:
                                    parse(val)
                                # if it continues to the next line that means it successfully parsed the object
                                # and it's some sort of time-related object
                                bad_key_paths.append(sub_key_path)
                        except (ValueError, OverflowError):
                            pass
            return bad_key_paths
        bad_keys = travel_dict(content)
        return bad_keys

    def update_problem_keys_file(self):
        '''Update the problem keys dictionary at the keys_filepath with new problematic keys
        '''
        existing_problem_keys = self.read_in_problematic_keys()
        for key, val in existing_problem_keys.items():
            if key == 'keys_to_replace':
                existing_problem_keys[key] = ' '.join(set(val.split()).union(self.json_keys))
            elif key == 'server_replay_ignore_payload_params':
                existing_problem_keys[key] = ' '.join(set(val.split()).union(self.form_keys))
            elif key == 'server_replay_ignore_params':
                existing_problem_keys[key] = ' '.join(set(val.split()).union(self.query_keys))
        self.write_out_problematic_keys(existing_problem_keys)

    def read_in_problematic_keys(self):
        '''Load problematic keys dictionary from the keys_filepath argument filepath in content-test-data repo
        if it exists. Otherwise, return the dictionary with empty values.
        '''
        print('executing "read_in_problematic_keys" method')
        repo_bad_keys_filepath = self.bad_keys_filepath.replace('/tmp/Mocks', 'content-test-data')
        print('reading in problematic keys data from "{}"'.format(repo_bad_keys_filepath))
        if path.exists(repo_bad_keys_filepath):
            with open(repo_bad_keys_filepath, 'r') as fp:
                problem_keys = json.load(fp)
        else:
            problem_keys = {
                'keys_to_replace': '',
                'server_replay_ignore_params': '',
                'server_replay_ignore_payload_params': ''
            }
        return problem_keys

    def write_out_problematic_keys(self, problem_keys: dict):
        '''Write updated problematic keys dictionary back to the file at the keys_filepath argument

        Args:
            problem_keys (dict): Updated dictionary of problematic keys
        '''
        with open(self.bad_keys_filepath, 'w') as bad_keys_file:
            bad_keys_file.write(json.dumps(problem_keys, indent=4))

    def load_problematic_keys(self):
        '''Load problematic keys from the keys_filepath argument filepath if it exists. Only necessary when running
        mitmdump in playback mode. Resets command line options with the key value pairs from the loaded dictionary.
        '''
        print('executing "load_problematic_keys" method')
        if path.exists(self.bad_keys_filepath):
            print('"{}" path exists - loading bad keys'.format(self.bad_keys_filepath))
            log_msg = 'options pre update: \nkeys_to_replace: {}'.format(ctx.options.keys_to_replace)
            log_msg += '\nserver_replay_ignore_params: {}'.format(ctx.options.server_replay_ignore_params)
            log_msg += '\nserver_replay_ignore_payload_params: {}'.format(
                ctx.options.server_replay_ignore_payload_params
            )
            print(log_msg)

            problem_keys = json.load(open(self.bad_keys_filepath, 'r'))
            # ctx.options.set(problem_keys.items())

            # need to do this because arguments for these options are interpreted as 1 list item
            query_keys = problem_keys.get('server_replay_ignore_params')
            ctx.options.server_replay_ignore_params = query_keys.split() if isinstance(query_keys, str) else query_keys
            form_keys = problem_keys.get('server_replay_ignore_payload_params')
            ctx.options.server_replay_ignore_payload_params = (
                form_keys.split() if isinstance(form_keys, str) else form_keys
            )
            keys_to_replace = problem_keys.get('keys_to_replace')
            # ctx.options.keys_to_replace = (
            #     keys_to_replace.split() if isinstance(keys_to_replace, str) else keys_to_replace
            # )
            ctx.options.keys_to_replace = keys_to_replace

            log_msg = 'options post update: \nkeys_to_replace: {}'.format(ctx.options.keys_to_replace)
            log_msg += '\nserver_replay_ignore_params: {}'.format(ctx.options.server_replay_ignore_params)
            log_msg += '\nserver_replay_ignore_payload_params: {}'.format(
                ctx.options.server_replay_ignore_payload_params
            )
            print(log_msg)
        else:
            print('"{}" path doesn\'t exist - no bad keys to set'.format(self.bad_keys_filepath))
            print('not setting bad keys from file')

    # def done(self):
    #     print('timestamp_replacer.py "done()" called')
    #     # print('ctx.options: \n{}'.format(json.dumps(ctx.options, indent=4)))
    #     if self.detect_timestamps:
    #         # bad_keys_filepath = ctx.options.keys_filepath
    #         all_keys = {
    #             'keys_to_replace': ' '.join(self.json_keys),
    #             'server_replay_ignore_payload_params': ' '.join(self.form_keys),
    #             'server_replay_ignore_params': ' '.join(self.query_keys)
    #         }
    #         with open(self.bad_keys_filepath, 'w') as bad_keys_file:
    #             bad_keys_file.write(json.dumps(all_keys))


addons = [TimestampReplacer()]
