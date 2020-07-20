"""
This module defines a RESTful HTTP server, the PYME Rule Server, which manages task distribution across a cluster through the use of rules.
Rules are essentially a json template which defines how tasks may be generated (by substitution into the template on the
client side). Generation of individual tasks on the client has the dual benefits of a) reducing the CPU load and memory
requirements on the server and b) dramatically decreasing the network bandwidth used for task distribution.
 
The exposed REST API is as follows:

============================================================= ====== ========================================
Endpoint                                                      Verb   Brief Description
============================================================= ====== ========================================
:meth:`/add_integer_id_rule <RuleServer.add_integer_id_rule>` POST   Add a new rule
:meth:`/release_rule_tasks <RuleServer.release_rule_tasks>`   POST   Release tasks IDs associated with a rule
:meth:`/task_advertisments <RuleServer.task_advertisements>`  GET    Retrieve a list of advertisements
:meth:`/bid_on_tasks <RuleServer.bid_on_tasks>`               POST   Submit bids on advertised tasks
:meth:`/handin <RuleServer.handin>`                           POST   Advise rule server of task completion
:meth:`/distributor/queues <Ruleserver.get_queues>`           GET    Get status information
============================================================= ====== ========================================

Implementation details follow. The json format and parameters for the REST calls are defined in the docstrings of the
python functions defining that method (linked from the table above). The server is launched by the ``PYMERuleServer``
command (see :mod:`PYME.cluster.PYMERuleServer`) which loads this in a separate thread and takes care of logging and zeroconf
registration, rather than by directly running this file.
"""
#import cherrypy
import threading
import requests
import queue as Queue
from six.moves import xrange

import logging
logging.basicConfig(level=logging.WARN)
logger = logging.getLogger('ruleserver')
logger.setLevel(logging.DEBUG)

import time
import sys
import ujson as json
#import json
import os

from PYME.misc import computerName
from PYME import config
#from PYME.IO import clusterIO
from PYME.util import webframework
import collections

import uuid

import numpy as np

class Rule(object):
    pass

STATUS_UNAVAILABLE, STATUS_AVAILABLE, STATUS_ASSIGNED, STATUS_COMPLETE, STATUS_FAILED = range(5)

class IntegerIDRule(Rule):
    """
    A rule which generates tasks based on a template.
    
    Parameters
    ----------
    ruleID : str
        A unique ID for this rule
    task_template : str
        A json template for generating tasks (see templates section below)
    inputs_by_task : list of dict
        A list of dictionaries mapping recipe variable names to URIs to treat as inputs (recipes only)
    max_task_ID : int
        The maximum number of tasks that can be generated by this rule
    task_timeout: float
        A timeout in seconds for each task. If a task takes longer than task_timeout seconds it is assumed that
        the node executing it has fallen over and we retry on a different node (up to a maximum number of times
        set by the 'ruleserver-retries` config option).
    rule_timeout : float
        A timeout in seconds from the last task we processed after which we assume that no more tasks are coming and
        we can delete the rule (to keep memory down in a long-term usage scenario).
    datasource_complete : bool
        Whether the data this rule applies to is complete (True) or still being
        generated (False). `datasource_complete==False` will block the rule from
        being marked as finished, and can be updated using 
        `self.mark_datasource_complete`.
        
    Notes
    -----
    
    *Templates*
    
    Templates are a string which can be substituted to generate tasks. There are currently two supported formats for
    templates, those for localization tasks, and those for recipes. Each take the form of a json dictionary:
    
    **Localization**
    
    .. code-block:: json
        
        {
         "id" : "{{ruleID}}~{{taskID}}",
         "type" : "localization",
         "taskdef" : {"frameIndex" : {{taskID}}, "metadata" : "PYME-CLUSTER://path/to/series/metadata.json"},
         "inputs" : {"frames" : "PYME-CLUSTER://path/to/series.pcs"},
         "outputs" : {"fitResults" : "PYME-CLUSTER://__aggregate_h5r/path/to/analysis.h5r/FitResults",
                      "driftResults" : "PYME-CLUSTER://__aggregate_h5r/path/to/analysis.h5r/DriftResults"}
        }
    
    **Recipes**
    The recipe can either be specified inline:
    
    .. code-block:: json
        
        {
         "id" : "{{ruleID}}~{{taskID}}",
         "type" : "recipe",
         "taskdef" : {"recipe" : "<recipe as a YAML string>"},
         "inputs" : {{taskInputs}},
         "output_dir" : "PYME-CLUSTER://path/to/output/dir",
        }
        
    or using a cluster URI:
    
    .. code-block:: json
        
        {
         "id" : "{{ruleID}}~{{taskID}}",
         "type" : "recipe",
         "taskdefRef" : "PYME-CLUSTER://path/to/recipe.yaml",
         "inputs" : {{taskInputs}},
         "output_dir" : "PYME-CLUSTER://path/to/output/dir",
        }
        
    The rule will substitute ``{{taskInputs}}`` with a dictionary mapping integer task IDs to recipe input files, e.g.
    
    .. code-block:: json
    
        {0 : {"recipe_input_0" : "input_0_URI_0","recipe_input_1" : "input_1_URI_0"},
         1 : {"recipe_input_0" : "input_0_URI_1","recipe_input_1" : "input_1_URI_1"},
         2 : {"recipe_input_0" : "input_0_URI_2","recipe_input_1" : "input_1_URI_2"},
         }
         
    Alternatively the inputs dictionary can be supplied directly (without relying on the taskInputs substitution).
    
    *Rule Chaining*
    
    Rules may also define a chained rule, to be run on completion of the original rule. This is accomplished by
    supplying an "on_completion" dictionary. This is a dictionary, ``{'template' : <template>, 'max_tasks' : max_tasks,
    'rule_timeout' : timeout, 'on_completion' : {...}}``, with the template following the format above and everything
    but the template being optional (max_tasks defaults to 1). Follow on / chained rules are slightly more restricted
    than standard rules in that recipe "inputs" must be hardcoded (no ``{{taskInputs}}`` substitution).
    
    Chained rules are created once the original rule is finished (see 
    `IntegerIDRule.finished`). All tasks for the chained rule will be released 
    immediately.
        
    """
    TASK_INFO_DTYPE = np.dtype([('status', 'uint8'), ('nRetries', 'uint8'), ('expiry', 'f4'), ('cost', 'f4')])
    
    
    def __init__(self, ruleID, task_template, inputs_by_task = None,
                 max_task_ID=100000, task_timeout=600, rule_timeout=3600, 
                 on_completion=None, datasource_complete=True):
        self.ruleID = ruleID
        
        if not inputs_by_task is None:
            self._inputs_by_task = {int(k):v for k, v in inputs_by_task.items()}
        else:
            self._inputs_by_task = None
            
        self._template = task_template
        self._task_info = np.zeros(max_task_ID, self.TASK_INFO_DTYPE)
        
        self._n_retries =  config.get('ruleserver-retries', 3)
        self._timeout = task_timeout
        
        self._rule_timeout = rule_timeout
        self._cached_advert = None
        self._active = True # making this rule inactive will cause it not to generate adverts (this is the closest we  get to aborting)
        
        self.nTotal = 0
        self.nAssigned = 0
        self.nAvailable = 0
        self.nCompleted = 0
        self.nFailed = 0
        
        self._n_max = max_task_ID
        self._datasource_complete = datasource_complete
        
        self.on_completion = on_completion
        
        self.avCost = 0
        
        self.expiry = time.time() + self._rule_timeout
              
        self._info_lock = threading.Lock()
        self._advert_lock = threading.Lock()
    
    def mark_datasource_complete(self, n_max):
        """
        Update the number of tasks this rule can create and mark the datasource
        as complete such that the rule can be marked as finished.

        Parameters
        ----------
        n_max : int
            number of tasks this rule can create

        Raises
        ------
        ValueError
            If `n_max` is less than number of tasks already assigned.
        """
        with self._info_lock, self._advert_lock:
            # NOTE - context likely also holds RuleServer._advert_lock

            self._update_nums()
            if self.nAssigned > n_max:
                raise ValueError('max_tasks cannot be less than nAssigned')
            
            # extend _task_info array if necessary
            if n_max > len(self._task_info):
                extension = np.zeros(n_max - len(self._task_info), 
                                     self.TASK_INFO_DTYPE)
                self._task_info = np.concatenate([self._task_info, extension])
            
            self._n_max = n_max
        self._datasource_complete = True
        
    def _update_nums(self):
        self.nAvailable = int((self._task_info['status'] == STATUS_AVAILABLE).sum())
        self.nAssigned = int((self._task_info['status'] == STATUS_ASSIGNED).sum())
        
    def _update_cost(self):
        av_cost = np.mean(self._task_info['cost'][self._task_info['status'] > STATUS_AVAILABLE])
        if np.isnan(av_cost):
            av_cost = 0
        else:
            av_cost = float(av_cost)
            
        self.avCost = av_cost
        
    def make_range_available(self, start, end):
        """
        Make a range of tasks available (to be called once the underlying data is available)
        [start, end)
        Parameters
        ----------
        start : int
            first task number to release (inclusive)
        end : int
            last task number to release (exclusive)
        Raises
        ------
        RuntimeError
            if asked to release a range which is invalid for the max tasks we can create from this rule
        """

        if start < 0 or start > self._task_info.size or end < 0 or end > self._task_info.size:
            raise RuntimeError('Range (%d, %d) invalid with maxTasks=%d' % (start, end, self._task_info.size))
        
        #TODO - check existing status - it probably makes sense to only apply this to tasks which have STATUS_UNAVAILABLE
        with self._info_lock:
            self._task_info['status'][start:end] = STATUS_AVAILABLE
            
            self.nTotal = int((self._task_info['status'] >0).sum())
        
            self._update_nums()

        self.expiry = time.time() + self._rule_timeout
        
        with self._advert_lock:
            self._cached_advert = None
            
    def bid(self, bid):
        """Bid on tasks (and return any that match). Note the current implementation is very naive and doesn't
        check bid cost - i.e. the first bid gets the task. This works if (and only if) the clients are well behaved
        and preferentially bid on tasks which have a lowest cost for them.
        
        Parameters
        ----------
        
        bid : dict
            A dictionary containing the ruleID, the IDs of the tasks to bid on, and their costs
            ``{"ruleID" : str ,"taskIDs" : [list of int],"taskCosts" : [list of float]}``
        
        Returns
        -------
        
        successful_bids: dict
            A dictionary containing the ruleID, the IDs of the tasks awarded, and the rule template
            ``{"ruleID" : ruleID, "taskIDs": [list of int], "template" : "<rule template>"}``
        
        """
        if not self._active:
            # don't accept any bids
            return {'ruleID': bid['ruleID'], 'taskIDs':[], 'template' : ''}
        
        taskIDs = np.array(bid['taskIDs'], 'i')
        costs = np.array(bid['costs'], 'f4')
        with self._info_lock:
            successful_bid_mask = self._task_info['status'][taskIDs] == STATUS_AVAILABLE
            successful_bid_ids = taskIDs[successful_bid_mask]
            self._task_info['status'][successful_bid_ids] = STATUS_ASSIGNED
            self._task_info['cost'][successful_bid_ids] = costs[successful_bid_mask]
            self._task_info['expiry'][successful_bid_ids] = time.time() + self._timeout
            
            nTasks = len(successful_bid_ids)
            self.nAvailable -= nTasks
            self.nAssigned += nTasks
            
            self._update_cost()

        self.expiry = time.time() + self._rule_timeout
        
        with self._advert_lock:
            self._cached_advert = None
            
        return {'ruleID': bid['ruleID'], 'taskIDs':successful_bid_ids.tolist(), 'template' : self._template}
    
    def mark_complete(self, info):
        """
        Mark a set of tasks as completed and/or failed
        
        Parameters
        ----------
        info : dict
            A dictionary of the form: ``{"ruleID": str, "taskIDs" : [list of int], "status" : [list of int]}``
            
            There should be an entry in status for each entry in taskIDs, the valid values of status being
            ``STATUS_COMPLETE=3`` or ``STATUS_FAILED=4``.

        Returns
        -------

        """
        taskIDs = np.array(info['taskIDs'], 'i')
        status = np.array(info['status'], 'uint8')
        
        with self._info_lock:
            self._task_info['status'][taskIDs] = status
            
            self.nCompleted += int((status ==STATUS_COMPLETE).sum())
            self.nFailed += int((status == STATUS_FAILED).sum())
            
            nTasks = len(taskIDs)
            self.nAssigned -= nTasks

        self.expiry = time.time() + self._rule_timeout
            
    @property
    def advert(self):
        """ The task advertisment.
        
        If the rule has tasks available, a dictionary of the form:
        ``{"ruleID" : str, "taskTemplate" : str, "availableTaskIDs" : [list of int], "inputsByTask" : [optional] dict mapping task IDs to inputs}``
        
        "inputsByTask" is only provided for some recipe tasks.
        """
        if not self._active:
            return None
        
        with self._advert_lock:
            if not self._cached_advert:
                availableTasks = np.where(self._task_info['status'] == STATUS_AVAILABLE)[0].tolist()
                
                if len(availableTasks) == 0:
                    self._cached_advert = None
                else:
                    self._cached_advert = {'ruleID' : self.ruleID,
                        'taskTemplate': self._template,
                        'availableTaskIDs': availableTasks}
                    
                    #print self._inputs_by_task
                    
                    if not self._inputs_by_task is None:
                        self._cached_advert['inputsByTask'] = {taskID: self._inputs_by_task[taskID] for taskID in availableTasks}
                
            return self._cached_advert
    
    # @property
    # def nAvailable(self):
    #     return (self._task_info['status'] == STATUS_AVAILABLE).sum()
    #
    # @property
    # def nAssigned(self):
    #     return (self._task_info['status'] == STATUS_ASSIGNED).sum()

    # @property
    # def nCompleted(self):
    #     return (self._task_info['status'] == STATUS_COMPLETE).sum()

    # @property
    # def nFailed(self):
    #     return (self._task_info['status'] == STATUS_FAILED).sum()

    
    @property
    def expired(self):
        """ Whether the rule has expired (no available tasks, no tasks assigned, and time > expiry) and can be removed"""
        return (self.nAvailable == 0) and (self.nAssigned == 0) and (time.time() > self.expiry)
    
    @property
    def finished(self):
        """ 
        Whether the rule has finished (datasource is marked as complete and
        the maximum number of tasks which can be created have been completed).
        """
        return self._datasource_complete and (self.nCompleted >= self._n_max)
    
    def inactivate(self):
        """
        Mark rule as inactive (generates no adverts) to facilitate aborting / pausing long-running rules.
        """
        self._active = False
    
    def info(self):
        """
        Get information / status about this rule
        
        Returns
        -------
        
        status : dict
            A status dictionary of the form:
            ``{'tasksPosted': int, 'tasksRunning': int, 'tasksCompleted': int, 'tasksFailed' : int, 'averageExecutionCost' : float}``

        """
        return {'tasksPosted': self.nTotal,
                  'tasksRunning': self.nAssigned,
                  'tasksCompleted': self.nCompleted,
                  'tasksFailed' : self.nFailed,
                  'averageExecutionCost' : self.avCost,
                  'active' : self._active
                }
    
    def poll_timeouts(self):
        t = time.time()
        
        with self._info_lock:
            timed_out = np.where((self._task_info['status'] == STATUS_ASSIGNED)*(self._task_info['expiry'] < t))[0]
            
            
            nTimedOut = len(timed_out)
            if nTimedOut > 0:
                self._task_info['status'][timed_out] = STATUS_AVAILABLE
                self._task_info['nRetries'][timed_out] += 1
                
                self.nAssigned -= nTimedOut
                self.nAvailable += nTimedOut
    
                retry_failed = self._task_info['nRetries'] > self._n_retries
                self._task_info['status'][retry_failed] = STATUS_FAILED
                self.nAvailable -= int(retry_failed.sum())
                
                #self._update_nums()
            
        with self._advert_lock:
            self._cached_advert = None
        
        
    
        


class RuleServer(object):
    MAX_ADVERTISEMENTS = 5 * 10 * 50 * 12 #only advertise enough for 100 tasks on each core of each cluster node
    def __init__(self):
        self._rules = collections.OrderedDict()
        
        #cherrypy.engine.subscribe('stop', self.stop)
        
        self._do_poll = True
        
        self._queueLock = threading.Lock()
        
        self._advert_lock = threading.Lock()
        
        #set up threads to poll the distributor and announce ourselves and get and return tasks
        #self.pollThread = threading.Thread(target=self._poll)
        #self.pollThread.start()
        
        self._cached_advert = None
        self._cached_advert_expiry = 0
        
        
        self._info_lock = threading.Lock()
        self._cached_info = None
        self._cached_info_expiry = 0
        self._cached_info_timeout = 5
        
        self._rule_n = 0
        
        self._rule_lock = threading.Lock() # lock for when we modify the dictionary of rules
        
        self.rulePollThread = threading.Thread(target=self._poll_rules)
        self.rulePollThread.start()
        
        with open(os.path.splitext(__file__)[0] + '.html', 'r') as f:
            self._status_page = f.read()
    
    
    def _poll(self):
        while self._do_poll:
            time.sleep(1)
    
    def _poll_rules(self):
        while self._do_poll:
            for qn in list(self._rules.keys()):
                self._rules[qn].poll_timeouts()
                
                # look for rules that have processed all tasks
                if self._rules[qn].finished:
                    with self._rule_lock:
                        r = self._rules.pop(qn)
                        
                    follow_on = r.on_completion
                    if follow_on is not None:
                        # if a follow on rule is defined, add it
                        template = follow_on['template']
                        n_tasks = follow_on.get('max_tasks', 1)
                        timeout = follow_on.get('rule_timeout', 3600.)
                        ruleID = '%06d-%s' % (self._rule_n, uuid.uuid4().hex)
    
                        rule = IntegerIDRule(ruleID, template, max_task_ID=int(n_tasks),
                                             rule_timeout=float(timeout), on_completion=follow_on.get('on_completion', None))
    
                        rule.make_range_available(0, int(n_tasks))
    
                        with self._rule_lock:
                            self._rules[ruleID] = rule
    
                        self._rule_n += 1
                    
                
                #remore queue if expired (no activity for an hour) to free up memory
                elif self._rules[qn].expired:
                    logger.debug('removing expired rule: %s' % self._rules[qn].ruleID)
                    with self._rule_lock:
                        r = self._rules.pop(qn)
                        
            
            time.sleep(5)
    
    def stop(self):
        self._do_poll = False
        
        #for queue in self._queues.values():
        #    queue.stop()
            
    @webframework.register_endpoint('/task_advertisements')
    def task_advertisements(self):
        """
        HTTP endpoint (GET) for retrieving task advertisements.
        
        Note - by default, a limited number of advertisements are posted at any given time (to limit bandwidth). This
        should be enough to keep all the workers busy, with new adverts being posted once tasks are assigned to workers.
        
        Returns
        -------
        
        adverts : json str
            List of advertisements. See :attr:`IntegerIDRule.advert`

        """
        with self._advert_lock:
            t = time.time()
            if (t > self._cached_advert_expiry):
                adverts = []
                nTasks = 0
                ruleN = 0
                
                rules = list(self._rules.values())
                while ruleN < len(rules) and nTasks < self.MAX_ADVERTISEMENTS:
                    advert = rules[ruleN].advert
                    
                    if not advert is None:
                        adverts.append(advert)
                        nTasks += len(advert['availableTaskIDs'])
                        
                    ruleN += 1
                    
                self._cached_advert = json.dumps(adverts)
                self._cached_advert_expiry = time.time() + 1 #regenerate advert once every second
            
        return self._cached_advert
        
        
    @webframework.register_endpoint('/bid_on_tasks')
    def bid_on_tasks(self, body=''):
        """
        HTTP endpoint (POST) for bidding on tasks.
        
        Parameters
        ----------
        body : json list of bids
            A list of bids, each of which is a dictionary of the form
            ``{"ruleID" : str ,"taskIDs" : [list of int],"taskCosts" : [list of float]}``
            see :meth:`IntegerIDRule.Bid` for details.

        Returns
        -------
        assignments: json str
            A json formatted list of task assignments, each of which has the form:
            ``{"ruleID" : ruleID, "taskIDs": [list of int], "template" : "<rule template>"}``
            See :meth:`IntegerIDTask.bid`

        """
        bids = json.loads(body)
        
        succesfull_bids = []
        
        for bid in bids:
            rule = self._rules[bid['ruleID']]
            
            succesfull_bids.append(rule.bid(bid))
            #task_ids = bid['taskIDs']
            #costs = bid['taskCosts']
            
        #print(succesfull_bids)
        return json.dumps(succesfull_bids)
        
            
        
    @webframework.register_endpoint('/add_integer_id_rule')
    def add_integer_id_rule(self, max_tasks= 1e6, release_start=None, release_end = None, ruleID = None, timeout=3600, body=''):
        """
        HTTP endpoint (POST) for adding a new rule.
        
        Add a rule that generates tasks based on an integer ID, such as a frame number (localization) or an index into
        a list of inputs (recipes). By default, tasks are not released on creation, but later with
        :func:`release_rule_tasks()`. This allows for the creation of a rule before a series has finished spooling.
        
        Parameters
        ----------
        max_tasks : int
            The maximum number of tasks that could match this rule. Generally the number of frames in a localization
            series (if known in advance) or the number of inputs on which to run a recipe. Allows us to pre-allocate a
            task array of the correct size (passing this rather than relying on the default reduces memory usage).
        release_start, release_end : int
            Release a range of tasks for computation after creating the rule (avoids a
            call to `release_rule_tasks()`
        ruleID : str
            A unique identifier for the rule. If not provided, one will be automatically generated
        timeout : int
            How long this rule should live for, in seconds. Defaults to an hour.
        body : str
            A json dictionary  ``{'template' : "<rule template>", 'inputsByTask' : [list of URIs]}``. See :class:`IntegerIDRule`
            for the format of the rule template. The ``inputsByTask`` parameter is only used for recipes, and can be omitted
            for localisation analysis, or for recipe tasks using a hard coded ``"inputs"`` dictionary. An optional additional
            parameter, "on_completion" parameter may be given, itself consisting of a new
            ``{'template': <template>, 'on_completion': {...}}`` dictionary.

        Returns
        -------
        
        result : json str
            The result of adding the rule. A dict of the form:
            ``{"ok" : "True", 'ruleID' : str}``

        """
        rule_info = json.loads(body)
        
        if ruleID is None:
            ruleID = '%06d-%s' % (self._rule_n, uuid.uuid4().hex)
        
        rule = IntegerIDRule(ruleID, rule_info['template'], max_task_ID=int(max_tasks), rule_timeout=float(timeout),
                             inputs_by_task=rule_info.get('inputsByTask', None), on_completion=rule_info.get('on_completion', None))
        
        #print rule._inputs_by_task
        if not release_start is None:
            rule.make_range_available(int(release_start), int(release_end))
        
        with self._rule_lock:
            self._rules[ruleID] = rule
        
        self._rule_n += 1
        
        return json.dumps({'ok': 'True', 'ruleID' : ruleID})

    @webframework.register_endpoint('/release_rule_tasks')
    def release_rule_tasks(self, ruleID, release_start, release_end, body=''):
        """
        HTTP Endpoint (POST or GET ) for releasing tasks associated with a rule.
        
        When performing spooling data analysis we typically have one rule per series and one task per frame.
        This method allows for the tasks associated with a particular frame (or range of frames) to be released as they
        become available.
        
        Parameters
        ----------
        ruleID : str
            The rule ID
        release_start, release_end: int
            The range of tasks IDs to release
        body : str, empty
            Needed for interface compatibility, ignored

        Returns
        -------
        
        success : json str
            ``{"ok" : "True"}`` if successful.

        """
        rule = self._rules[ruleID]
    
        rule.make_range_available(int(release_start), int(release_end))
    
    
        return json.dumps({'ok': 'True'})
    
    @webframework.register_endpoint('/inactivate_rule')
    def inactivate_rule(self, ruleID):
        self._rules[ruleID].inactivate()

        return json.dumps({'ok': 'True'})
    
    @webframework.register_endpoint('/handin')
    def handin(self, body):
        """
        HTTP Endpoint (POST) to mark tasks as having been completed
        
        Parameters
        ----------
        body : json list
            A list of entries of the form ``{"ruleID": str, "taskIDs" : [list of int], "status" : [list of int]}``
            where status has the same length as taskIDs with each entry being either ``STATUS_COMPLETE`` or
            ``STATUS_FAILED``. See :meth:`IntegerIDRule.mark_complete`.

        Returns
        -------
        
        success : json str
            ``{"ok" : "True"}`` if successful.
        

        """
        
        #logger.debug('Handing in tasks...')
        try:
            for handin in json.loads(body):
                ruleID = handin['ruleID']
                
                rule = self._rules[ruleID]
                
                rule.mark_complete(handin)
            return json.dumps({'ok': True})
        except KeyError as e:  # rule may have expired
            return json.dumps({'ok': False, 'error': str(e)})
    
    @webframework.register_endpoint('/mark_datasource_complete')
    def mark_datasource_complete(self, rule_id, n_max):
        """
        
        HTTP Endpoint (POST) to update the number of max tasks which can be
        created from a given rule and mark its datasource as complete so the
        rule can eventually be marked as finished. 
        
        Parameters
        ----------
        rule_id : str
            ID of the rule to update
        n_max : int
            max tasks which should be created from the rule
        
        Returns
        -------
        success : str
            ``{"ok" : "True"}`` if successful.
        
        Raises
        ------
        ValueError
            If `value` is less than number of tasks already assigned.
        """
        # take out the rule lock in case we are still creating the rule and the
        # client POSTs this (e.g. if a series is started/stopped quickly)
        with self._rule_lock:
            self._rules[rule_id].mark_datasource_complete(int(n_max))
        return json.dumps({'ok': 'True'})
    
    @webframework.register_endpoint('/distributor/queues')
    def get_queues(self):
        """
        HTTP Endpoint (GET) - visible at "distributor/queues" - for querying ruleserver status.
        
        Returns
        -------
        
        status: json str
            A dictionary of the form ``{"ok" : True, "result" : {ruleID0 : rule0.info(), ruleID1 : rule1.info()}}``
            See :meth:`IntegerIDRule.info`.
        """
        with self._info_lock:
            t = time.time()
            if (t > self._cached_info_expiry):
                with self._rule_lock:
                    self._cached_info = json.dumps({'ok': True, 'result': {qn: self._rules[qn].info() for qn in self._rules.keys()}})
                self._cached_info_expiry = time.time() + self._cached_info_timeout
                
        return self._cached_info
    
    @webframework.register_endpoint('/queue_info_longpoll')
    def get_queue_info(self):
        """
        a throttled version of queue info
        
        Returns
        -------

        """
        time.sleep(0.5)
        return self.get_queues()
    
    @webframework.register_endpoint('/', mimetype='text/html')
    def status(self):
        return self._status_page


# class CPRuleServer(RuleServer):
#     @cherrypy.expose
#     def add_integer_rule(self, queue=None, nodeID=None, numWant=50, timeout=5):
#         cherrypy.response.headers['Content-Type'] = 'application/json'
#
#         body = ''
#         #if cherrypy.request.method == 'GET':
#
#         if cherrypy.request.method == 'POST':
#             body = cherrypy.request.body.read()
#
#         return self._tasks(queue, nodeID, numWant, timeout, body)
#
#     @cherrypy.expose
#     def handin(self, nodeID):
#         cherrypy.response.headers['Content-Type'] = 'application/json'
#
#         body = cherrypy.request.body.read()
#
#         return self._handin(nodeID, body)
#
#     @cherrypy.expose
#     def announce(self, nodeID, ip, port):
#         cherrypy.response.headers['Content-Type'] = 'application/json'
#
#         self._announce(nodeID, ip, port)
#
#     @cherrypy.expose
#     def queues(self):
#         cherrypy.response.headers['Content-Type'] = 'application/json'
#
#         return self._get_queues()


class WFRuleServer(webframework.APIHTTPServer, RuleServer):
    """
    Combines the RuleServer with it's web framework.
    
    Largely an artifact of initial experiments using cherrypy (allowed quickly switching between cherrypy
    and our internal webframework).
    """
    def __init__(self, port, bind_addr=''):
        RuleServer.__init__(self)
        
        server_address = (bind_addr, port)
        webframework.APIHTTPServer.__init__(self, server_address)
        self.daemon_threads = True


# def runCP(port):
#     import socket
#     cherrypy.config.update({'server.socket_port': port,
#                             'server.socket_host': '0.0.0.0',
#                             'log.screen': False,
#                             'log.access_file': '',
#                             'log.error_file': '',
#                             'server.thread_pool': 50,
#                             })
#
#     logging.getLogger('cherrypy.access').setLevel(logging.ERROR)
#
#     #externalAddr = socket.gethostbyname(socket.gethostname())
#
#     distributor = CPRuleServer()
#
#     app = cherrypy.tree.mount(distributor, '/distributor/')
#     app.log.access_log.setLevel(logging.ERROR)
#
#     try:
#
#         cherrypy.quickstart()
#     finally:
#         distributor._do_poll = False


import threading

class ServerThread(threading.Thread):
    """"""
    def __init__(self, port, bind_addr='', profile=False):
        self.port = int(port)
        self._profile = profile
        self.bind_addr = bind_addr
        threading.Thread.__init__(self)
        
    def run(self):
        """"""
        
        if self._profile:
            from PYME.util import mProfile
        
            mProfile.profileOn(['ruleserver.py', ])
            profileOutDir = config.get('dataserver-root', os.curdir) + '/LOGS/%s/mProf' % computerName.GetComputerName()

        if self.bind_addr == '':
            import socket
            self.externalAddr = socket.gethostbyname(socket.gethostname())
        else:
            self.externalAddr = self.bind_addr
            
        self.distributor = WFRuleServer(self.port, bind_addr=self.bind_addr)

        logger.info('Starting ruleserver on %s:%d' % (self.externalAddr, self.port))
        try:
            self.distributor.serve_forever()
        finally:
            self.distributor._do_poll = False
            #logger.info('Shutting down ...')
            #self.distributor.shutdown()
            logger.info('Closing server ...')
            self.distributor.server_close()

            if self._profile:
                mProfile.report(False, profiledir=profileOutDir)
            
    
    def shutdown(self):
        self.distributor._do_poll = False
        logger.info('Shutting down ...')
        self.distributor.shutdown()
        logger.info('Closing server ...')
        self.distributor.server_close()


def on_SIGHUP(signum, frame):
    """"""
    from PYME.util import mProfile
    mProfile.report(False, profiledir=profileOutDir)
    raise RuntimeError('Recieved SIGHUP')


if __name__ == '__main__':
    import signal
    
    port = sys.argv[1]
    
    if (len(sys.argv) == 3) and (sys.argv[2] == '-k'):
        profile = True
        from PYME.util import mProfile
        
        mProfile.profileOn(['ruleserver.py', ])
        profileOutDir = config.get('dataserver-root', os.curdir) + '/LOGS/%s/mProf' % computerName.GetComputerName()
    else:
        profile = False
        profileOutDir = None
    
    if not sys.platform == 'win32':
        #windows doesn't support handling signals ... don't catch and hope for the best.
        #Note: This will make it hard to cleanly shutdown the distributor on Windows, but should be OK for testing and
        #development
        signal.signal(signal.SIGHUP, on_SIGHUP)
    
    try:
        run(int(port))
    finally:
        if profile:
            mProfile.report(False, profiledir=profileOutDir)
        
