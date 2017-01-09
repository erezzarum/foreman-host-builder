#!/usr/bin/env python

import sys
import time
import Queue
import threading
import signal
import logging
import logging.config
from optparse import OptionParser
import ConfigParser
import base64
import yaml
from foreman.client import Foreman, ForemanException


def setup_logging():
    global log
    logging.config.fileConfig('logging.conf')
    log = logging.getLogger('main')


class SignalHandler:
    def __init__(self, workers):
        self.log = logging.getLogger('SignalHandler')
        self.workers = workers

    def __call__(self, signum, frame):
        for worker in self.workers:
            worker.terminate()
            worker.join()
        self.log.info('Terminated by Signal: %s' % signum)


class HostWorker(threading.Thread):
    def __init__(self, queue, cfg):
        threading.Thread.__init__(self)
        self.log = logging.getLogger(type(self).__name__)
        self.stophandler = threading.Event()
        self.queue = queue
        self.cfg = cfg

    def run(self):
        while not self.stophandler.isSet():
            try:
                host = self.queue.get_nowait()
            except Queue.Empty:
                self.log.debug('Queue is empty, we are done here!')
                break
            try:
                create_host(cfg=self.cfg, host=host)
            except Exception, e:
                self.log.exception(e)

    def terminate(self):
        self.log.debug('%s terminate was called, terminating!' % self.name)
        self.stophandler.set()


def create_host(cfg, host):
    stime = time.time()

    log.info('Creating host: %s' % host['name'])
    log.debug('Host %s: %s' % (host['name'], host))

    if 'timeout' in cfg:
        timeout = int(cfg['timeout'])
    else:
        timeout = 60

    if 'timeout_post' in cfg:
        timeout_post = int(cfg['timeout_post'])
    else:
        timeout_post = 600

    if 'timeout_delete' in cfg:
        timeout_delete = int(cfg['timeout_delete'])
    else:
        timeout_delete = 600

    if 'use_cache' in cfg:
        use_cache = cfg['use_cache']
    else:
        use_cache = True

    if 'verify' in cfg:
        verify = cfg['verify']
    else:
        verify = False

    foreman_api = Foreman(url=cfg['server'],
                          auth=(cfg['username'], cfg['password']),
                          api_version=cfg['api_version'],
                          timeout=timeout,
                          timeout_post=timeout_post,
                          timeout_delete=timeout_delete,
                          use_cache=use_cache,
                          verify=verify
                          )
    host_dict = dict()

    # quick hack to check if hosts exists
    # TODO: check if domain provided via yaml/hostgroup
    _results = foreman_api.hosts.index(search=
                                       'name ~ %s.%%' % host['name'])['results']
    if _results:
        log.warn('Host %s already exists with id: %s' % (host['name'],
                                                         _results[0]['id']))
        return True

    for key in host:
        if key == 'hostgroup':
            hostgroup_id = foreman_api.hostgroups.show(
                id=host['hostgroup'])['id']
            if hostgroup_id:
                host_dict['hostgroup_id'] = hostgroup_id
            else:
                log.warn('Could not match an id for hostgroup: %s' %
                         host['hostgroup'])
        elif key == 'subnet':
            subnet_id = foreman_api.subnets.show(id=host['subnet'])['id']
            if subnet_id:
                host_dict['subnet_id'] = subnet_id
            else:
                log.warn('Could not match an id for subnet: %s' %
                         host['subnet'])
        elif key == 'compute_profile':
            compute_profile_id = foreman_api.compute_profiles.show(
                id=host['compute_profile'])['id']
            if compute_profile_id:
                host_dict['compute_profile_id'] = compute_profile_id
            else:
                log.warn('Could not match an id for compute_profile: %s' %
                         host['compute_profile'])
        elif key == 'host_parameters':
            host_dict['host_parameters_attributes'] = host['host_parameters']
        else:
            host_dict[key] = host[key]

    log.debug('%s host_dict: %s' % (host['name'], host_dict))

    try:
        foreman_api.hosts.create(host=host_dict)
    except ForemanException, e:
        log.error('%s ForemanException: %s' % (host['name'], e))

    log.info('%s created, took: %.2fs' % (host['name'], (time.time() - stime)))
    return True


def config_parser(filename):
    log.debug('Trying to parse config file: %s' % filename)
    cfg = ConfigParser.SafeConfigParser()
    cfg_dict = {}
    if not cfg.read(filename):
        log.info('Configuration file %s was not found, creating it' % filename)
        cfg.add_section('foreman')
        cfg.set('foreman', 'server', 'https://localhost')
        cfg.set('foreman', 'username', 'admin')
        cfg.set('foreman', 'password', base64.b64encode('admin'))
        cfg.set('foreman', 'api_version', '2')
        try:
            with open(filename, 'w+') as fh:
                cfg.write(fh)
        except Exception, e:
            log.exception('Failed creating configuration file %s: %s' % (
                filename, e))
            sys.exit(1)

    sections = cfg.sections()
    for section in sections:
        for name, value in cfg.items(section):
            if value == '':
                log.error('%s is set, but empty' % name)
                sys.exit(1)
            if name == 'password':
                cfg_dict['password'] = base64.b64decode(value)
            else:
                cfg_dict[name] = value
    return cfg_dict


def yaml_template_parser(filename):
    log.debug('Trying to parse YAML file: %s' % filename)
    try:
        with open(filename, 'r') as fh:
            lines = fh.read()
    except IOError, e:
        log.critical('Failed parsing YAML file %s: %s' % (filename, e))
        sys.exit(1)
    except Exception, e:
        log.exception('Unhandled exception caught: %s' % e)
        sys.exit(2)

    results = yaml.load(lines)
    log.debug('YAML file %s: %s' % (filename, results))
    return results


def parse_options():
    optparser = OptionParser()
    optparser.add_option('--config', dest='config',
                         help='config file',
                         type='string', default='foreman-host-builder.cfg')
    optparser.add_option('--template', dest='template',
                         help='YAML template',
                         type='string', default='foreman-host-builder.yaml')
    optparser.add_option('--threads', dest='threads',
                         help='Sets the number of concurrent threads',
                         type='int', default=8)

    (opts, args) = optparser.parse_args()
    return opts, args


def main():
    (opts, args) = parse_options()
    setup_logging()
    stime = time.time()
    log.info('Starting Foreman host builder')
    log.debug(opts)
    cfg = config_parser(opts.config)
    hosts = yaml_template_parser(opts.template)['hosts']
    log.info('Hosts: %i, %s' % (len(hosts), [x for x in hosts]))
    log.debug('hosts: %s' % hosts)

    queue = Queue.Queue()

    for host in hosts:
        hosts[host]['name'] = host
        queue.put(hosts[host])

    workers = []
    for i in range(opts.threads):
        worker = HostWorker(queue, cfg)
        worker.setName('Worker%s' % i)
        worker.setDaemon(True)
        workers.append(worker)

    handler = SignalHandler(workers)
    signal.signal(signal.SIGINT, handler)

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    took = time.time() - stime
    log.info('Done!, took: %.2fs' % took)


if __name__ == '__main__':
    main()
