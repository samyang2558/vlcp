'''
Created on 2016/4/13

:author: hubo
'''
from vlcp.service.sdn.flowbase import FlowBase
from vlcp.server.module import depend, ModuleNotification, callAPI
import vlcp.service.sdn.ofpportmanager as ofpportmanager
import vlcp.service.sdn.ovsdbportmanager as ovsdbportmanager
import vlcp.service.kvdb.objectdb as objectdb
from vlcp.event.event import Event, withIndices
from vlcp.event.runnable import RoutineContainer, RoutineException
from vlcp.config.config import defaultconfig
from vlcp.service.sdn.ofpmanager import FlowInitialize
from vlcp.utils.networkmodel import PhysicalPort, LogicalPort, PhysicalPortSet, LogicalPortSet, LogicalNetwork, PhysicalNetwork
from vlcp.utils.flowupdater import FlowUpdater
from vlcp.event.connection import ConnectionResetException
from vlcp.protocol.openflow.openflow import OpenflowConnectionStateEvent,\
    OpenflowErrorResultException
from pprint import pformat
from namedstruct import dump

@withIndices('datapathid', 'vhost', 'connection')
class LogicalPortChanged(Event):
    pass

@withIndices('datapathid', 'vhost', 'connection')
class PhysicalPortChanged(Event):
    pass

@withIndices('datapathid', 'vhost', 'connection')
class LogicalNetworkChanged(Event):
    pass

@withIndices('datapathid', 'vhost', 'connection')
class PhysicalNetworkChanged(Event):
    pass

_events = (LogicalPortChanged, PhysicalPortChanged, LogicalNetworkChanged, PhysicalNetworkChanged)

class IDAssigner(object):
    def __init__(self):
        self._indices = {}
        self._revindices = {}
        # Reserve 0 and 0xffff
        self._revindices[0] = '<reserve0>'
        self._revindices[0xffff] = '<reserve65535>'
        self._lastindex = 1
    def assign(self, key):
        if key in self._indices:
            return self._indices[key]
        else:
            ind = self._lastindex
            while ind in self._revindices:
                ind += 1
                ind &= 0xffff
            self._revindices[ind] = key
            self._indices[key] = ind
            self._lastindex = ind + 1
            return ind
    def unassign(self, keys):
        for k in keys:
            ind = self._indices.pop(k, None)
            if ind is not None:
                del self._revindices[ind]
    def frozen(self):
        return dict(self._indices)

class IOFlowUpdater(FlowUpdater):
    def __init__(self, connection, systemid, bridgename, parent):
        FlowUpdater.__init__(self, connection, (LogicalPortSet.default_key(),
                                                PhysicalPortSet.default_key()), ('ioprocessing', connection))
        self._walkerdict = {LogicalPortSet.default_key(): self._logicalport_walker,
                            PhysicalPortSet.default_key(): self._physicalport_walker
                            }
        self._systemid = systemid
        self._bridgename = bridgename
        self._portnames = {}
        self._portids = {}
        self._currentportids = {}
        self._currentportnames = {}
        self._lastportids = {}
        self._lastportnames = {}
        self._lastnetworkids = {}
        self._networkids = IDAssigner()
        self._phynetworkids = IDAssigner()
        self._physicalnetworkids = {}
        self._logicalportkeys = set()
        self._physicalportkeys = set()
        self._logicalnetworkkeys = set()
        self._physicalnetworkkeys = set()
        self._parent = parent
    def update_ports(self, ports, ovsdb_ports):
        self._portnames.clear()
        self._portnames.update((p['name'], p['ofport']) for p in ovsdb_ports)
        self._portids.clear()
        self._portids.update((p['id'], p['ofport']) for p in ovsdb_ports if p['id'])
        for m in self.restart_walk():
            yield m
    def _logicalport_walker(self, key, value, walk, save):
        save(key)
        logset = value.set
        for id in self._portids:
            logports = logset.find(LogicalPort, id)
            for p in logports:
                try:
                    logp = walk(p.getkey())
                except KeyError:
                    pass
                else:
                    save(logp.getkey())
                    try:
                        lognet = walk(logp.network.getkey())
                    except KeyError:
                        pass
                    else:
                        save(lognet.getkey())
                        try:
                            phynet = walk(lognet.physicalnetwork.getkey())
                        except KeyError:
                            pass
                        else:
                            save(phynet.getkey())
    def _physicalport_walker(self, key, value, walk, save):
        save(key)
        physet = value.set
        for name in self._portnames:
            phyports = physet.find(PhysicalPort, self._connection.protocol.vhost, self._systemid, self._bridgename, name)
            # There might be more than one match physical port rule for one port, pick the most specified one
            namedict = {}
            for p in phyports:
                _, inds = PhysicalPort._getIndices(p.getkey())
                name = inds[-1]
                ind_key = [i != '%' for i in inds]
                if name != '%':
                    if name in namedict:
                        if namedict[name][0] < ind_key:
                            namedict[name] = (ind_key, p)
                    else:
                        namedict[name] = (ind_key, p)
            phyports = [v[1] for v in namedict.values()]
            for p in phyports:
                try:
                    phyp = walk(p.getkey())
                except KeyError:
                    pass
                else:
                    save(phyp.getkey())
                    try:
                        phynet = walk(phyp.physicalnetwork.getkey())
                    except KeyError:
                        pass
                    else:
                        save(phynet.getkey())
    def walkcomplete(self, keys, values):
        conn = self._connection
        dpid = conn.openflow_datapathid
        vhost = conn.protocol.vhost
        _currentportids = dict(self._portids)
        _currentportnames = dict(self._portnames)
        for cls, ev, name, idg, assigner in ((LogicalPort, LogicalPortChanged, '_logicalportkeys', lambda x: _currentportids[x.id], None),
                                 (PhysicalPort, PhysicalPortChanged, '_physicalportkeys', lambda x: _currentportnames[x.name], None),
                                 (LogicalNetwork, LogicalNetworkChanged, '_logicalnetworkkeys', lambda x: self._networkids.assign(x.getkey()), self._networkids),
                                 (PhysicalNetwork, PhysicalNetworkChanged, '_physicalnetworkkeys', lambda x: self._phynetworkids.assign(x.getkey()), self._phynetworkids),
                                 ):
            objs = [v for v in values if v.isinstance(cls)]
            objkeys = set([v.getkey() for v in objs])
            oldkeys = getattr(self, name)
            if objkeys != oldkeys:
                if assigner is not None:
                    assigner.unassign(oldkeys.difference(objkeys))
                setattr(self, name, objkeys)
                for m in self.waitForSend(ev(dpid, vhost, conn, current = [(o, idg(o)) for o in objs])):
                    yield m
        self._currentportids = _currentportids
        self._currentportnames = _currentportnames
    def updateflow(self, connection, addvalues, removevalues, updatedvalues):
        # We must do these in order, each with a batch:
        # 1. Remove flows
        # 2. Remove groups
        # 3. Add groups, modify groups
        # 4. Add flows, modify flows
        try:
            cmds = []
            ofdef = connection.openflowdef
            vhost = connection.protocol.vhost
            input_table = self._parent._gettableindex('ingress', vhost)
            input_next = self._parent._getnexttable('', 'ingress', vhost = vhost)
            output_table = self._parent._gettableindex('egress', vhost)
            # Cache all IDs, save them into last. We will need them for remove.
            _lastportids = self._lastportids
            _lastportnames = self._lastportnames
            _lastnetworkids = self._lastnetworkids
            _portids = dict(self._currentportids)
            _portnames = dict(self._currentportnames)
            _networkids = self._networkids.frozen()
            # We must generate actions from network driver
            phyportset = [obj for obj in self._savedresult if obj.isinstance(PhysicalPort)]
            phynetset = [obj for obj in self._savedresult if obj.isinstance(PhysicalNetwork)]
            lognetset = [obj for obj in self._savedresult if obj.isinstance(LogicalNetwork)]
            logportset = [obj for obj in self._savedresult if obj.isinstance(LogicalPort)]
            # If a port is both a logical port and a physical port, flows may conflict.
            # Remove the port from dictionary if it is duplicated.
            logportofps = set(_portids[lp.id] for lp in logportset if lp.id in _portids)
            _portnames = dict((n,v) for n,v in _portnames.items() if v not in logportofps)
            self._lastportids = _portids
            self._lastportnames = _portnames
            self._lastnetworkids = _networkids
            # Group current ports by network for further use
            phyportdict = {}
            for p in phyportset:
                phyportdict.setdefault(p.physicalnetwork, []).append(p)
            lognetdict = {}
            for n in lognetset:
                lognetdict.setdefault(n.physicalnetwork, []).append(n)
            logportdict = {}
            for p in logportset:
                logportdict.setdefault(p.network, []).append(p)
            allapis = []
            for pnet in phynetset:
                if pnet in lognetdict and pnet in phyportdict:
                    for lognet in lognetdict[pnet]:
                        netid = _networkids.get(lognet.getkey())
                        if netid is not None:
                            for p in phyportdict[pnet]:
                                if lognet in addvalues or lognet in updatedvalues or p in addvalues or p in updatedvalues:
                                    pid = _portnames.get(p.name)
                                    if pid is not None:
                                        def subr(lognet, p, netid, pid):
                                            try:
                                                for m in callAPI(self, 'public', 'createioflowparts', {'connection': connection,
                                                                                                       'logicalnetwork': lognet,
                                                                                                       'physicalport': p,
                                                                                                       'logicalnetworkid': netid,
                                                                                                       'physicalportid': pid}):
                                                    yield m
                                            except Exception:
                                                self._parent._logger.warning("Create flow parts failed for %r and %r", lognet, p, exc_info = True)
                                                self.retvalue = None
                                            else:
                                                self.retvalue = ((lognet, p), self.retvalue)
                                        allapis.append(subr(lognet, p, netid, pid))
            for m in self.executeAll(allapis):
                yield m
            flowparts = dict(r[0] for r in self.retvalue if r[0] is not None)
            def execute_commands():
                if cmds:
                    try:
                        for m in connection.protocol.batch(cmds, connection, self):
                            yield m
                    except OpenflowErrorResultException:
                        self._parent._logger.warning("Some Openflow commands return error result on connection %r, will ignore and continue.\n"
                                                     "Details:\n%s", connection,
                                                     "\n".join("REQUEST = \n%s\nERRORS = \n%s\n" % (pformat(dump(k)), pformat(dump(v)))
                                                               for k,v in self.openflow_replydict.items()))
                    del cmds[:]
            if connection.protocol.disablenxext:
                # Nicira extension is disabled, use metadata instead
                # 64-bit metadata is used as:
                # | 16-bit input network | 16-bit output network | 16-bit reserved | 16-bit output port |
                # When first initialized, input network = output network = Logical Network no.
                # output port = OFPP_ANY, reserved bits are 0xFFFF
                def create_input_instructions(lognetid, extra_actions):
                    lognetid = (lognetid & 0xffff)
                    instructions = [ofdef.ofp_instruction_write_metadata(
                                        metadata = (lognetid << 48) | (lognetid << 32) | (0xffff << 16) | (ofdef.OFPP_ANY & 0xffff)
                                    ),
                                    ofdef.ofp_instruction_goto_table(table_id = input_next)
                                    ]
                    if extra_actions:
                        instructions.insert(0, ofdef.ofp_instruction_actions(actions = list(extra_actions)))
                    return instructions
                def create_output_oxm(lognetid, portid):
                    return [ofdef.create_oxm(ofdef.OXM_OF_METADATA_W, (portid & 0xFFFF) | ((lognetid & 0xFFFF) << 32), 0x0000FFFF0000FFFF)]
            else:
                # With nicira extension, we store input network, output network and output port in REG4, REG5 and REG6
                def create_input_instructions(lognetid, extra_actions):
                    lognetid = (lognetid & 0xffff)
                    return [ofdef.ofp_instruction_actions(actions = [
                                    ofdef.nx_action_reg_load(
                                            ofs_nbits = ofdef.create_ofs_nbits(0, 32),
                                            dst = ofdef.NXM_NX_REG4,
                                            value = lognetid
                                            ),
                                    ofdef.nx_action_reg_load(
                                            ofs_nbits = ofdef.create_ofs_nbits(0, 32),
                                            dst = ofdef.NXM_NX_REG5,
                                            value = lognetid
                                            ),
                                    ofdef.nx_action_reg_load(
                                            ofs_nbits = ofdef.create_ofs_nbits(0, 32),
                                            dst = ofdef.NXM_NX_REG6,
                                            value = ofdef.OFPP_ANY
                                            )
                                ] + list(extra_actions)),
                            ofdef.ofp_instruction_goto_table(table_id = input_next)
                            ]
                def create_output_oxm(lognetid, portid):
                    return [ofdef.create_oxm(ofdef.NXM_NX_REG5, lognetid),
                            ofdef.create_oxm(ofdef.NXM_NX_REG6, portid)]
            for obj in removevalues:
                if obj.isinstance(LogicalPort):
                    ofport = _lastportids.get(obj.id)
                    if ofport is not None:
                        cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                 ofport
                                                                                 )])
                                                       ))
                        cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofport,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm()))
                elif obj.isinstance(PhysicalPort):
                    ofport = _lastportnames.get(obj.name)
                    if ofport is not None:
                        cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                 ofport
                                                                                 )])
                                                       ))
                        cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                       cookie = 0x000100000000000 | (ofport << 16),
                                                       cookie_mask = 0xffffffffffff0000,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm()))
                elif obj.isinstance(LogicalNetwork):
                    groupid = _lastnetworkids.get(obj.getkey())
                    if groupid is not None:
                        cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                       cookie = 0x0001000000000000 | groupid,
                                                       cookie_mask = 0xffffffffffffffff,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm()
                                                       ))
                        cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                       cookie = 0x0001000000000000 | groupid,
                                                       cookie_mask = 0xffff00000000ffff,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm()
                                                       ))
                        cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = groupid,
                                                       match = ofdef.ofp_match_oxm()))
            # Never use flow mod to update an input flow of physical port, because the input_oxm may change.
            for obj in updatedvalues:
                if obj.isinstance(PhysicalPort):
                    ofport = _lastportnames.get(obj.name)
                    if ofport is not None:
                        cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                 ofport
                                                                                 )])
                                                       ))
                elif obj.isinstance(LogicalNetwork):
                    groupid = _lastnetworkids.get(obj.getkey())
                    if groupid is not None:
                        cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                       cookie = 0x0001000000000000 | groupid,
                                                       cookie_mask = 0xffffffffffffffff,
                                                       command = ofdef.OFPFC_DELETE,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm()
                                                       ))
            for m in execute_commands():
                yield m
            for obj in removevalues:
                if obj.isinstance(LogicalNetwork):
                    groupid = _lastnetworkids.get(obj.getkey())
                    if groupid is not None:
                        cmds.append(ofdef.ofp_group_mod(command = ofdef.OFPGC_DELETE,
                                                        type = ofdef.OFPGT_ALL,
                                                        group_id = groupid
                                                        ))
            for m in execute_commands():
                yield m
            def create_buckets(obj):
                # Generate buckets
                buckets = [ofdef.ofp_bucket(actions=[ofdef.ofp_action_output(port = _portids[p.id])])
                           for p in logportdict[obj]
                           if p.id in _portids]
                if obj.physicalnetwork in phyportdict:
                    for p in phyportdict[obj.physicalnetwork]:
                        if (obj, p) in flowparts:
                            fp = flowparts[(obj,p)]
                            buckets.append(ofdef.ofp_bucket(actions=list(fp[3])))
                return buckets
            for obj in addvalues:
                if obj.isinstance(LogicalNetwork):
                    groupid = _networkids.get(obj.getkey())
                    if groupid is not None:
                        cmds.append(ofdef.ofp_group_mod(command = ofdef.OFPGC_ADD,
                                                        type = ofdef.OFPGT_ALL,
                                                        group_id = groupid,
                                                        buckets = create_buckets(obj)
                                                        ))
            # Updated networks when:
            # 1. Network is updated
            # 2. Physical network of this logical network is updated
            # 3. Logical port is added or removed from the network
            # 4. Physical port is added or removed from the physical network
            otherupdates = set([obj for obj in updatedvalues if obj.isinstance(LogicalNetwork)])
            otherupdates.update(obj.network for obj in addvalues if obj.isinstance(LogicalPort))
            #otherupdates.update(obj.network for obj in updatedvalues if obj.isinstance(LogicalPort))
            otherupdates.update(obj.network for obj in removevalues if obj.isinstance(LogicalPort))
            updated_physicalnetworks = set(obj for obj in updatedvalues if obj.isinstance(PhysicalNetwork))
            updated_physicalnetworks.update(p.physicalnetwork for p in addvalues if p.isinstance(PhysicalPort))
            updated_physicalnetworks.update(p.physicalnetwork for p in removevalues if p.isinstance(PhysicalPort))
            updated_physicalnetworks.update(p.physicalnetwork for p in updatedvalues if p.isinstance(PhysicalPort))
            otherupdates.update(lnet for pnet in updated_physicalnetworks
                                 if pnet in lognetdict
                                 for lnet in lognetdict[p.physicalnetwork])
            for obj in otherupdates:
                groupid = _networkids.get(obj.getkey())
                if groupid is not None:
                    cmds.append(ofdef.ofp_group_mod(command = ofdef.OFPGC_MODIFY,
                                                    type = ofdef.OFPGT_ALL,
                                                    group_id = groupid,
                                                    buckets = create_buckets(obj)
                                                    ))
            for m in execute_commands():
                yield m
            # There are 5 kinds of flows:
            # 1. in_port = (Logical Port)
            # 2. in_port = (Physical_Port), network = (Logical_Network)
            # 3. out_port = (Logical Port)
            # 4. out_port = (Physical_Port), network = (Logical_Network)
            # 5. out_port = OFPP_ANY, network = (Logical_Network)
            for obj in addvalues:
                if obj.isinstance(LogicalPort):
                    ofport = _portids.get(obj.id)
                    lognetid = _networkids.get(obj.network.getkey())
                    if ofport is not None and lognetid is not None:
                        cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                       command = ofdef.OFPFC_ADD,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                 ofport
                                                                                 )]),
                                                       instructions = create_input_instructions(lognetid, [])
                                                       ))
                        cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                       command = ofdef.OFPFC_ADD,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm(oxm_fields = create_output_oxm(lognetid, ofport)),
                                                       instructions = [ofdef.ofp_instruction_actions(actions = [
                                                                    ofdef.ofp_action_output(port = ofport)
                                                                    ])]
                                                       ))
            # Ignore update of logical port
            # Physical port:
            for obj in addvalues:
                if obj.isinstance(PhysicalPort):
                    ofport = _portnames.get(obj.name)
                    if ofport is not None and obj.physicalnetwork in lognetdict:
                        for lognet in lognetdict[obj.physicalnetwork]:
                            lognetid = _networkids.get(lognet.getkey())
                            if lognetid is not None and (lognet, obj) in flowparts:
                                input_oxm, input_actions, output_actions, _ = flowparts[(lognet, obj)]
                                cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                               cookie = 0x0001000000000000 | lognetid,
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_ADD,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                        ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                         ofport
                                                                                         )] + input_oxm),
                                                               instructions = create_input_instructions(lognetid, input_actions)
                                                               ))
                                cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                               cookie = 0x0001000000000000 | lognetid | (ofport << 16),
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_ADD,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = create_output_oxm(lognetid, ofport)),
                                                               instructions = [ofdef.ofp_instruction_actions(actions = 
                                                                            list(output_actions))]
                                                               ))
            for lognet in addvalues:
                if lognet.isinstance(LogicalNetwork):
                    lognetid = _networkids.get(lognet.getkey())
                    if lognetid is not None and lognet.physicalnetwork in phyportdict:
                        for obj in phyportdict[lognet.physicalnetwork]:
                            ofport = _portnames.get(obj.name)
                            if ofport is not None and (lognet, obj) in flowparts and obj not in addvalues:
                                input_oxm, input_actions, output_actions, _ = flowparts[(lognet, obj)]
                                cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                               cookie = 0x0001000000000000 | lognetid,
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_ADD,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                        ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                         ofport
                                                                                         )] + input_oxm),
                                                               instructions = create_input_instructions(lognetid, input_actions)
                                                               ))
                                cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                               cookie = 0x0001000000000000 | lognetid | (ofport << 16),
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_ADD,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = create_output_oxm(lognetid, ofport)),
                                                               instructions = [ofdef.ofp_instruction_actions(actions = 
                                                                            list(output_actions))]
                                                               ))
            for obj in updatedvalues:
                if obj.isinstance(PhysicalPort):
                    ofport = _portnames.get(obj.name)
                    if ofport is not None and obj.physicalnetwork in lognetdict:
                        for lognet in lognetdict[obj.physicalnetwork]:
                            lognetid = _networkids.get(lognet.getkey())
                            if lognetid is not None and (lognet, obj) in flowparts and not lognet in addvalues:
                                input_oxm, input_actions, output_actions, _ = flowparts[(lognet, obj)]
                                cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                               cookie = 0x0001000000000000 | lognetid,
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_ADD,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                        ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                         ofport
                                                                                         )] + input_oxm),
                                                               instructions = create_input_instructions(lognetid, input_actions)
                                                               ))
                                cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                               cookie = 0x0001000000000000 | lognetid | (ofport << 16),
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_MODIFY,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = create_output_oxm(lognetid, ofport)),
                                                               instructions = [ofdef.ofp_instruction_actions(actions = 
                                                                            list(output_actions))]
                                                               ))
            for lognet in updatedvalues:
                if lognet.isinstance(LogicalNetwork):
                    lognetid = _networkids.get(lognet.getkey())
                    if lognetid is not None and lognet.physicalnetwork in phyportdict:
                        for obj in phyportdict[lognet.physicalnetwork]:
                            ofport = _portnames.get(obj.name)
                            if ofport is not None and (lognet, obj) in flowparts and obj not in addvalues and obj not in updatedvalues:
                                input_oxm, input_actions, output_actions, _ = flowparts[(lognet, obj)]
                                cmds.append(ofdef.ofp_flow_mod(table_id = input_table,
                                                               cookie = 0x0001000000000000 | lognetid,
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_ADD,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = [
                                                                        ofdef.create_oxm(ofdef.OXM_OF_IN_PORT,
                                                                                         ofport
                                                                                         )] + input_oxm),
                                                               instructions = create_input_instructions(lognetid, input_actions)
                                                               ))
                                cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                               cookie = 0x0001000000000000 | lognetid | (ofport << 16),
                                                               cookie_mask = 0xffffffffffffffff,
                                                               command = ofdef.OFPFC_MODIFY,
                                                               priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                               buffer_id = ofdef.OFP_NO_BUFFER,
                                                               out_port = ofdef.OFPP_ANY,
                                                               out_group = ofdef.OFPG_ANY,
                                                               match = ofdef.ofp_match_oxm(oxm_fields = create_output_oxm(lognetid, ofport)),
                                                               instructions = [ofdef.ofp_instruction_actions(actions = 
                                                                            list(output_actions))]
                                                               ))
            # Logical network broadcast
            for lognet in addvalues:
                if lognet.isinstance(LogicalNetwork):
                    lognetid = _networkids.get(lognet.getkey())
                    if lognetid is not None:
                        cmds.append(ofdef.ofp_flow_mod(table_id = output_table,
                                                       command = ofdef.OFPFC_ADD,
                                                       priority = ofdef.OFP_DEFAULT_PRIORITY,
                                                       buffer_id = ofdef.OFP_NO_BUFFER,
                                                       out_port = ofdef.OFPP_ANY,
                                                       out_group = ofdef.OFPG_ANY,
                                                       match = ofdef.ofp_match_oxm(oxm_fields = create_output_oxm(lognetid, ofdef.OFPP_ANY)),
                                                       instructions = [ofdef.ofp_instruction_actions(actions =
                                                                            [ofdef.ofp_action_group(group_id = lognetid)])]
                                                       ))
            # Ignore logical network update
            for m in execute_commands():
                yield m
        except Exception:
            self._parent._logger.warning("Update flow for connection %r failed with exception", connection, exc_info = True)
            # We don't want the whole flow update stops, so ignore the exception and continue
    
@defaultconfig
@depend(ofpportmanager.OpenflowPortManager, ovsdbportmanager.OVSDBPortManager, objectdb.ObjectDB)
class IOProcessing(FlowBase):
    "Ingress and Egress processing"
    _tablerequest = (("ingress", (), ''),
                     ("egress", ("ingress",),''))
    _default_vhostmap = {}
    def __init__(self, server):
        FlowBase.__init__(self, server)
        self.apiroutine = RoutineContainer(self.scheduler)
        self.apiroutine.main = self._main
        self.routines.append(self.apiroutine)
        self._flowupdaters = {}
        self._portchanging = set()
        self._portchanged = set()
    def _main(self):
        flow_init = FlowInitialize.createMatcher(_ismatch = lambda x: self.vhostbind is None or x.vhost in self.vhostbind)
        port_change = ModuleNotification.createMatcher("openflowportmanager", "update", _ismatch = lambda x: self.vhostbind is None or x.vhost in self.vhostbind)
        while True:
            yield (flow_init, port_change)
            if self.apiroutine.matcher is flow_init:
                c = self.apiroutine.event.connection
                self.apiroutine.subroutine(self._init_conn(self.apiroutine.event.connection))
            else:
                if self.apiroutine.event.reason == 'disconnected':
                    self.apiroutine.subroutine(self._remove_conn(c))
                else:
                    e = self.apiroutine.event
                    c = e.connection
                    self.apiroutine.subroutine(self._portchange(c))
    def _init_conn(self, conn, lastlist = None):
        # Default drop
        for m in conn.protocol.batch((conn.openflowdef.ofp_flow_mod(table_id = self._gettableindex("ingress", conn.protocol.vhost),
                                                           command = conn.openflowdef.OFPFC_ADD,
                                                           priority = 0,
                                                           buffer_id = conn.openflowdef.OFP_NO_BUFFER,
                                                           match = conn.openflowdef.ofp_match_oxm(),
                                                           instructions = [conn.openflowdef.ofp_instruction_actions(
                                                                            type = conn.openflowdef.OFPIT_CLEAR_ACTIONS
                                                                            )]
                                                           ),
                                      conn.openflowdef.ofp_flow_mod(table_id = self._gettableindex("egress", conn.protocol.vhost),
                                                           command = conn.openflowdef.OFPFC_ADD,
                                                           priority = 0,
                                                           buffer_id = conn.openflowdef.OFP_NO_BUFFER,
                                                           match = conn.openflowdef.ofp_match_oxm(),
                                                           instructions = [conn.openflowdef.ofp_instruction_actions(
                                                                            type = conn.openflowdef.OFPIT_CLEAR_ACTIONS
                                                                            )]
                                                           )), conn, self.apiroutine):
            yield m
        if conn in self._flowupdaters:
            self._flowupdaters[conn].close()
        datapath_id = conn.openflow_datapathid
        ovsdb_vhost = self.vhostmap.get(conn.protocol.vhost, "")
        for m in callAPI(self.apiroutine, 'ovsdbmanager', 'waitbridgeinfo', {'datapathid': datapath_id,
                                                                            'vhost': ovsdb_vhost}):
            yield m
        bridgename, systemid, _ = self.apiroutine.retvalue            
        new_updater = IOFlowUpdater(conn, systemid, bridgename, self)
        self._flowupdaters[conn] = new_updater
        new_updater.start()
        for m in self._portchange(conn):
            yield m
    def _remove_conn(self, conn):
        # Do not need to modify flows
        if conn in self._flowupdaters:
            self._flowupdaters[conn].close()
            del self._flowupdaters[conn]
        if False:
            yield
    def _portchange(self, conn):
        # Do not re-enter
        if conn in self._portchanging:
            self._portchanged.add(conn)
            raise StopIteration
        self._portchanging.add(conn)
        try:
            while True:
                self._portchanged.discard(conn)
                flow_updater = self._flowupdaters.get(conn)
                if flow_updater is None:
                    break
                datapath_id = conn.openflow_datapathid
                ovsdb_vhost = self.vhostmap.get(conn.protocol.vhost, "")
                for m in callAPI(self.apiroutine, 'openflowportmanager', 'getports', {'datapathid': datapath_id,
                                                                                      'vhost': conn.protocol.vhost}):
                    yield m
                ports = self.apiroutine.retvalue
                if conn in self._portchanged:
                    continue
                def ovsdb_info():
                    while True:
                        try:
                            if conn in self._portchanged:
                                self.apiroutine.retvalue = None
                                raise StopIteration
                            for m in self.apiroutine.executeAll([callAPI(self.apiroutine, 'ovsdbportmanager', 'waitportbyno', {'datapathid': datapath_id,
                                                                                                   'vhost': ovsdb_vhost,
                                                                                                   'portno': p.port_no,
                                                                                                   })
                                                                 for p in ports]):
                                yield m
                        except Exception:
                            self._logger.warning("OVSDB connection may not be ready for datapathid %016x, vhost = %r", datapath_id, ovsdb_vhost, exc_info = True)
                            while True:
                                trytimes = 0
                                try:
                                    for m in callAPI(self.apiroutine, 'ovsdbmanager', 'waitconnection', {'datapathid': datapath_id,
                                                                                                         'vhost': ovsdb_vhost}):
                                        yield m
                                except Exception:
                                    trytimes += 1
                                    if trytimes > 10:
                                        self._logger.warning("OVSDB connection is still not ready after a long time for %016x, vhost = %r. Keep waiting...", datapath_id, ovsdb_vhost)
                                        trytimes = 0
                                else:
                                    break
                        else:
                            break
                    self.apiroutine.retvalue = [r[0] for r in self.apiroutine.retvalue]
                conn_down = conn.protocol.statematcher(conn)
                try:
                    for m in self.apiroutine.withException(ovsdb_info(), conn_down):
                        yield m
                except RoutineException:
                    self._portchanged.discard(conn)
                    raise StopIteration
                if conn in self._portchanged:
                    continue
                ovsdb_ports = self.apiroutine.retvalue
                flow_updater = self._flowupdaters.get(conn)
                if flow_updater is None:
                    break
                for m in flow_updater.update_ports(ports, ovsdb_ports):
                    yield m
                if conn not in self._portchanged:
                    break
        finally:
            self._portchanging.remove(conn)
