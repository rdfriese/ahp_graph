"""
This module implements support for AHP (Attributed Hierarchical Port) graphs.

This class of graph supports attributes on the nodes (Devices), links that are
connected to named ports on the nodes, and nodes that may be represented by a
hierarchical graph (aka an assembly).
All links are bidirectional.
"""

import os
import collections
import pygraphviz
from .Device import *


class DeviceGraph:
    """
    A DeviceGraph is a graph of Devices and their connections to one another.

    The Devices are nodes and the links connect the ports on the nodes.
    This implements an AHP (Attributed Hierarchical Port) graph.
    """

    def __init__(self, attr: dict = None) -> 'DeviceGraph':
        """
        Define an empty DeviceGraph.

        The attributes are considered global parameters shared by all instances
        in the graph. They are only supported at the top-level graph,
        not intemediate graphs (e.g., assemblies).
        The dictionary of links uses a sorted tuple of DevicePorts as the key
        """
        self.expanding = None
        self.attr = attr if attr is not None else dict()
        self.devices = dict()
        self.links = dict()
        self.ports = set()

    def __repr__(self) -> str:
        """Pretty print a DeviceGraph with Devices followed by links."""
        lines = list()
        for device in self.devices.values():
            lines.append(str(device))
        for (p0, p1), t in self.links.items():
            lines.append(f"{p0} <--{t}--> {p1}")
        return "\n".join(lines)

    def link(self, p0: DevicePort, p1: DevicePort,
             latency: str = '0s') -> None:
        """
        Link two DevicePorts.

        Links are bidirectional and the key is a sorted tuple of the two
        DevicePorts. Duplicate links (links between the same DevicePorts)
        are not permitted. Keep in mind that a unique DevicePort is created
        for each port number in a multi-port style port. If the link
        types to not match, then throw an exception. Devices that are linked
        to will be added to the graph automatically.
        Latency is expressed as a string with time units (ps, ns, us...)
        """
        if callable(p0) or callable(p1):
            raise RuntimeError(f"{p0} or {p1} is callable. This probably means"
                               f" you have a multi port and didn't pick a port"
                               f" number (ex. Device.portX(portNum))")

        def get_other_port(port: 'DevicePort', dev: 'Device') -> tuple:
            """Get a matching port through an expanding assembly."""
            if port.link is not None:
                other = port.link
                # clear the link for each port and remove from the ports set
                port.link = None
                other.link = None
                self.ports -= {port, other}
                if port < other:
                    latency = self.links.pop((port, other))
                else:
                    latency = self.links.pop((other, port))
                # add the other device to the graph if it doesn't exist
                # so that we update the name properly
                if dev not in self.devices.values():
                    while dev.subOwner is not None:
                        dev = dev.subOwner
                    self.add(dev)
                return (other, latency)
            return (None, None)

        if self.expanding is not None:
            if p0.device == self.expanding:
                (p0, latency) = get_other_port(p0, p1.device)
                if p0 is None:
                    return
            elif p1.device == self.expanding:
                (p1, latency) = get_other_port(p1, p0.device)
                if p1 is None:
                    return

        if p0 in self.ports or p1 in self.ports:
            raise RuntimeError(f'{p0} or {p1} already linked to')

        devs = [p0.device, p1.device]
        # Check to make sure the port types match
        t0 = devs[0].get_portinfo()[p0.name]['type']
        t1 = devs[1].get_portinfo()[p1.name]['type']
        if t0 != t1:
            raise RuntimeError(
                f"Port type mismatch: {t0} != {t1} between"
                f"{devs[0].name} and {devs[1].name}"
            )

        # Add devices to the graph if they aren't already included
        for dev in devs:
            if dev not in self.devices.values():
                while dev.subOwner is not None:
                    dev = dev.subOwner
                self.add(dev)

        # Storing the ports in a set so that we can quickly see if they
        # are linked to already
        self.ports |= {p0, p1}
        if p1 < p0:
            link = (p1, p0)
        else:
            link = (p0, p1)
        # Only update the links if neither are connected
        # otherwise we are most likely doing a separate graph expansion and
        # don't want to overwrite the port links that exist
        if p0.link is None and p1.link is None:
            p0.link = p1
            p1.link = p0
        self.links[link] = latency

    def add(self, device: Device) -> None:
        """
        Add a Device to the graph.

        The Device must be a AHPGraph Device. The name must be unique.
        If the Device has submodules, then we add those, as well.
        Do NOT add submodules to a Device after you have added it using
        this function. Add submodules first, then add the parent.
        """
        if self.expanding is not None:
            device.name = f"{self.expanding.name}.{device.name}"
            if (self.expanding.partition is not None
                    and device.partition is None):
                device.partition = self.expanding.partition

        if device.name in self.devices:
            raise RuntimeError(f"Name already in graph: {device.name}")
        self.devices[device.name] = device
        for (dev, _, _) in device.subs:
            self.add(dev)

    def count_devices(self) -> dict:
        """
        Count the Devices in a graph.

        Return a map of Devices to integer counts. The keys are of the
        form "CLASS_MODEL".
        """
        counter = collections.defaultdict(int)
        for device in self.devices.values():
            counter[device.get_category()] += 1
        return counter

    def verify_links(self) -> None:
        """Verify that all required ports are linked up."""
        # Create a map of Devices to all ports linked on those Devices.
        d2ports = collections.defaultdict(set)
        for (p0, p1) in self.links:
            d2ports[p0.device].add(p0.name)
            d2ports[p1.device].add(p1.name)

        # Walk all Devices and make sure required ports are connected.
        for device in self.devices.values():
            portinfo = device.get_portinfo()
            for name in portinfo:
                if portinfo[name]['required'] and name not in d2ports[device]:
                    raise RuntimeError(f"{device.name} requires port {name}")

    def check_partition(self) -> None:
        """Check to make sure the graph has ranks specified for all Devices."""
        for d in self.devices.values():
            if d.partition is None:
                raise RuntimeError(f"No partition for Device: {d.name}")

    def follow_links(self, rank: int, prune: bool = False) -> None:
        """
        Chase links between ranks.

        Given a graph, follow links from the specified rank and expand
        assemblies until links are fully defined (links touch library
        Devices on both sides)
        Optional prune flag will remove unnecessary Devices and links from
        the graph. This will result in a different overall graph but will
        potentially save memory
        """
        self.check_partition()
        while True:
            devices = set()
            linksToRemove = set()
            for (p0, p1) in self.links:
                # One of the Devices is on the rank that we are
                # following links on
                if (p0.device.partition[0] == rank
                        or p1.device.partition[0] == rank):
                    for p in [p0, p1]:
                        if p.device.library is None:
                            devices.add(p.device)
                # This link is not used on our rank, remove it
                # Devices will not be inserted into the graph if they
                # aren't linked to
                elif prune:
                    linksToRemove.add((p0, p1))

            # Can't remove items from the dictionary while we are iterating
            # through it, so we store them in a set and remove them after
            # should be better than copying the dictionary
            for link in linksToRemove:
                del self.links[link]
                link[0].link = None
                link[1].link = None
                self.ports.discard(link[0])
                self.ports.discard(link[1])

            if devices:
                self.flatten(1, expand=devices)
            else:
                break

    def flatten(self, levels: int = None, name: str = None,
                rank: int = None, expand: set = None) -> None:
        """
        Recursively flatten the graph by the specified number of levels.

        For example, if levels is one, then only one level of the hierarchy
        will be expanded.  If levels is None, then the graph will be fully
        expanded.  If levels is zero or no Devices are an assembly, then just
        return self.

        The name parameter lets you flatten the graph only under a specified
        Device. This expansion allows for multilevel expansion of an assembly
        of assemblies since Devices that are created during expansion have
        the parent assembly's name prepended to their own

        The rank parameter lets you flatten the graph for all Devices in the
        specified rank

        You can also provide a set of Devices to expand instead of looking
        through the entire graph
        """
        # Devices must have a matching name if provided, a matching
        # rank if provided, and be within the expand set if provided
        if levels == 0:
            return

        assemblies = set()
        if name is not None:
            splitName = name.split(".")

        for dev in self.devices.values():
            assembly = dev.assembly

            # check to see if the name matches
            if name is not None:
                assembly &= splitName == dev.name.split(".")[0: len(splitName)]
            # rank to check
            if rank is not None:
                assembly &= rank == dev.partition[0]
            # check to see if the Device is in the expand set
            if expand is not None:
                assembly &= dev in expand

            if assembly:
                assemblies.add(dev)

        if not assemblies:
            return

        # Expand the required Devices
        for device in assemblies:
            self.expanding = device
            device.expand(self)
            del self.devices[device.name]
        self.expanding = None

        # Recursively flatten
        self.flatten(None if levels is None else levels-1, name, rank, expand)

    def write_dot(self, name: str, draw: bool = False, ports: bool = False,
                  hierarchy: bool = True) -> None:
        """
        Take a DeviceGraph and write it as a graphviz dot graph.

        All output will be stored in a folder called output
        The draw parameter will automatically generate SVGs if set to True
        The ports parameter will display ports on the graph if set to True

        The hierarchy parameter specifies whether you would like to view the
        graph as a hierarchy of assemblies or if you would like get a flat
        view of the graph as it is
        hierarchy is True by default, and highly recommended for large graphs
        """
        if not os.path.exists('output'):
            os.makedirs('output')

        if hierarchy:
            self.__write_dot_hierarchy(name, draw, ports)
        else:
            self.__write_dot_flat(name, draw, ports)

    def __write_dot_hierarchy(self, name: str,
                              draw: bool = False, ports: bool = False,
                              assembly: str = None, types: set = None) -> set:
        """
        Take a DeviceGraph and write dot files for each assembly.

        Write a graphviz dot file for each unique assembly (type, model) in the
        graph
        assembly and types should NOT be specified by the user, they are
        soley used for recursion of this function
        """
        graph = self.__format_graph(name, ports)
        if types is None:
            types = set()

        splitName = None
        splitNameLen = None
        if assembly is not None:
            splitName = assembly.split('.')
            splitNameLen = len(splitName)

        # Expand all unique assembly types and write separate graphviz files
        for dev in self.devices.values():
            if dev.assembly:
                category = dev.get_category()
                if category not in types:
                    types.add(category)
                    expanded = DeviceGraph()
                    dev.expand(expanded)
                    types = expanded.__write_dot_hierarchy(category, draw,
                                                           ports, dev.name,
                                                           types)

        # Need to check if the provided assembly name is in the graph
        # and if so make that our cluster
        if assembly is not None:
            for dev in self.devices.values():
                if assembly == dev.name:
                    # This device is the assembly that we just expanded
                    # make this a cluster and add its ports as nodes
                    clusterName = f"cluster_{dev.type}"
                    subgraph = graph.subgraph(name=clusterName, color='green')
                    for port in dev.ports:
                        graph.add_node(f"{dev.type}:{port}",
                                       shape='diamond', label=port,
                                       color='green', fontcolor='green')
                    break
        else:
            # No provided assembly, this is most likely the top level
            subgraph = graph

        # Loop through all Devices and add them to the graphviz graph
        for dev in self.devices.values():
            if assembly != dev.name:
                label = dev.name
                nodeName = dev.name
                if assembly is not None:
                    if splitName == dev.name.split('.')[0:splitNameLen]:
                        nodeName = '.'.join(dev.name.split('.')[splitNameLen:])
                        label = nodeName
                if dev.model is not None:
                    label += f"\\nmodel={dev.model}"
                if ports:
                    portLabels = dev.label_ports()
                    if portLabels != '':
                        label += f"|{portLabels}"

                # If the Device is an assembly, put a link to its SVG
                if dev.assembly:
                    subgraph.add_node(nodeName, label=label,
                                      href=f"{dev.get_category()}.svg",
                                      color='blue', fontcolor='blue')
                elif dev.subOwner is not None:
                    subgraph.add_node(nodeName, label=label,
                                      color='purple', fontcolor='purple')
                else:
                    subgraph.add_node(nodeName, label=label)

        self.__dot_add_links(graph, ports, assembly, splitName, splitNameLen)

        graph.write(f"output/{name}.dot")
        if draw:
            graph.draw(f"output/{name}.svg", format='svg', prog='dot')

        return types

    def __write_dot_flat(self, name: str, draw: bool = False,
                         ports: bool = False) -> None:
        """
        Write the DeviceGraph as a DOT file.

        It is suggested that you use write_dot_hierarchy for large graphs
        """
        graph = self.__format_graph(name, ports)

        for dev in self.devices.values():
            label = dev.name
            if dev.model is not None:
                label += f"\\nmodel={dev.model}"
            if ports:
                portLabels = dev.label_ports()
                if portLabels != '':
                    label += f"|{portLabels}"
            if dev.subOwner is not None:
                graph.add_node(dev.name, label=label,
                               color='purple', fontcolor='purple')
            else:
                graph.add_node(dev.name, label=label)

        self.__dot_add_links(graph, ports)

        graph.write(f"output/{name}.dot")
        if draw:
            graph.draw(f"output/{name}.svg", format='svg', prog='dot')

    @staticmethod
    def __format_graph(name: str, record: bool = False) -> pygraphviz.AGraph:
        """Format a new graph."""
        h = ('.edge:hover text {\n'
             '\tfill: red;\n'
             '}\n'
             '.edge:hover path, .node:hover polygon, .node:hover ellipse {\n'
             '\tstroke: red;\n'
             '\tstroke-width: 10;\n'
             '}')
        if not os.path.exists('output/highlightStyle.css'):
            with open('output/highlightStyle.css', 'w') as f:
                f.write(h)

        graph = pygraphviz.AGraph(strict=False, name=name)
        graph.graph_attr['stylesheet'] = 'highlightStyle.css'
        graph.node_attr['style'] = 'filled'
        graph.node_attr['fillcolor'] = '#EEEEEE'  # light gray fill
        graph.edge_attr['penwidth'] = '2'
        if record:
            graph.node_attr['shape'] = 'record'
            graph.graph_attr['rankdir'] = 'LR'

        return graph

    def __dot_add_links(self, graph: pygraphviz.AGraph,
                        ports: bool = False, assembly: str = None,
                        splitName: str = None, splitNameLen: int = 0) -> None:
        """Add edges to the graph with a label for the number of edges."""
        def port2Node(port: DevicePort) -> str:
            """Return a node name given a DevicePort."""
            node = port.device.name
            if node == assembly:
                return f"{port.device.type}:{port.name}"
            elif assembly is not None:
                if splitName == node.split('.')[0:splitNameLen]:
                    node = '.'.join(node.split('.')[splitNameLen:])
            if ports:
                return (node, port.name)
            else:
                return node

        # Create a list of all of the links
        links = list()
        for (p0, p1), latency in self.links.items():
            links.append((port2Node(p0), port2Node(p1)))

        # Setup a counter so we can check for duplicates
        duplicates = collections.Counter(links)
        for key in duplicates:
            label = ''
            if duplicates[key] > 1:
                label = duplicates[key]

            graphNodes = [key[0], key[1]]
            graphPorts = ['', '']
            for i in range(2):
                if type(key[i]) is tuple:
                    graphNodes[i] = key[i][0]
                    graphPorts[i] = key[i][1]
            # Add edges using the number of links as a label
            graph.add_edge(graphNodes[0], graphNodes[1], label=label,
                           tailport=graphPorts[0], headport=graphPorts[1])

        def device2Node(dev: Device) -> str:
            """Return a node name given a Device."""
            node = dev.name
            if assembly is not None:
                if splitName == node.split('.')[0:splitNameLen]:
                    node = '.'.join(node.split('.')[splitNameLen:])
            return node

        # Add "links" to submodules so they don't just float around
        for dev in self.devices.values():
            if dev.subOwner is not None:
                graph.add_edge(device2Node(dev), device2Node(dev.subOwner),
                               color='purple', style='dashed')
