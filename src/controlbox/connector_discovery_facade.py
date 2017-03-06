
"""
Discovery fires available/unavailable events for resources

Serial Resource connector turns the resource into a Serial instance, and then into a Connector
(sniffer from application). Posts Connector available event (connector not opened.)


"""
import logging
import time

from serial import Serial

from controlbox.conduit.discovery import PolledResourceDiscovery, ResourceAvailableEvent, ResourceUnavailableEvent
from controlbox.conduit.process_conduit import ProcessDiscovery
from controlbox.conduit.serial_conduit import SerialDiscovery
from controlbox.conduit.server_discovery import TCPServerDiscovery
from controlbox.connector.base import CloseOnErrorConnector, Connector, ConnectorError, ProtocolConnector
from controlbox.connector.processconn import ProcessConnector
from controlbox.connector.serialconn import SerialConnector
from controlbox.connector.socketconn import SocketConnector, TCPServerEndpoint
from controlbox.protocol.async import AsyncLoop
from controlbox.support.events import QueuedEventSource

from controlbox.support.retry_strategy import RetryStrategy, PeriodRetryStrategy

logger = logging.getLogger(__name__)


class MaintainedConnection:
    """
    Attempts to maintain a connection to an endpoint by checking if the connector is open, and
    attempting to open it if not.

    The ConnectorEvent instances fired from the connector are propagated to an event listener.

    The connection is managed synchronously by calling maintain() at regular intervals.
    For asynchronous management, use a MaintainedConnectorLoop, which will run the management on a separate
    thread.

    Fires ConnectorConnectedEvent and ConnectorDisconnectedEvent as the connection state changes.

    :param: resource    The resource corresponding to the connector. This is used only
        for logging/information.
    :param: connector   The connector to the endpoint to maintain. If this is closed,
        this managed connection attempts to open it after retry_preiod.
    :param: retry_period    How often to try opening the connection when it's closed
    :param: events          event source to post the resource events when the connection
        opens and closed. Should support the method fire(...)
     """
    # todo add a mixin for connector listener so the connector events
    # are hooked up in a consistent way
    def __init__(self, resource, connector: Connector, retry_strategy: RetryStrategy, events, log=logger):
        super().__init__()
        self.resource = resource        # an identifier for this managed connection
        self.connector = connector      # the connector that can provide a conduit to the endpoint
        connector.events.add(self._connector_events) # listen to the connector
        self.retry_strategy = retry_strategy
        self.events = events
        self.logger = log

    def _connector_events(self, *args, **kwargs):
        """ propagates connector events to the external events handler """
        self.events.fire(*args, **kwargs)

    def _open(self):
        """
        attempts to establish the connection.

        If the connection raises a connection error, it is logged, but not raised
        :return: True if the the connector was tried - the connector was not connected and available
        """
        connector = self.connector
        try_open = not connector.connected and connector.available
        if try_open:
            try:
                connector.connect()
                self.logger.info("device connected: %s" % self.resource)
            except ConnectorError as e:
                if (self.logger.isEnabledFor(logging.DEBUG)):
                    self.logger.exception(e)
                    self.logger.debug("Unable to connect to device %s: %s" % (self.resource, e))
        return try_open

    def _close(self):
        """
        Closes the connection to the connector.
        :return:
        """
        was_connected = self.connector.connected
        self.connector.disconnect()
        if was_connected:
            self.logger.info("device disconnected: %s" % self.resource)
        return was_connected

    def maintain(self, current_time=time.time):
        """
        Maintains the connection by attempting to open it if not already open.
        :param current_time: the current time. Used to
        determine if the connection was tried or not.
        :return: True if the connection was tried
        """
        delay = self.retry_strategy(current_time)
        will_try = delay <= 0
        if will_try:
            self._open()
        return will_try


class MaintainedConnectionLoop(AsyncLoop):
    """
    maintains the connection as a background thread.

    :param maintained_connection    The connection to maintain on a background thread
    :param loop A function to call while the connection is established
    """

    def __init__(self, maintained_connection, loop=None):
        super().__init__()
        self.maintained_connection = maintained_connection
        self._loop = loop

    def loop(self):
        """
        open the connector, and while connected,
        read responses from the protocol.

        :return:
        """
        maintained_connection = self.maintained_connection
        try:
            maintained_connection._open()
            while maintained_connection.connector.connected:
                success = False
                try:
                    time.sleep(0)
                    self._connected_loop()
                    success = True
                finally:
                    if not success:
                        maintained_connection._close()
        finally:
            self.stop_event.wait(maintained_connection.retry_strategy())

    def _connected_loop(self):
        """called repeatedly while the connection is open"""
        if self._loop:
            self._loop(self.maintained_connection)


class ConnectionManager:
    """
    Keeps track of the resources available for potential controllers, and attempts to open them
    at regular intervals. For each resource, a MaintaleinedConnection is used
    to keep that resource open. The

    A connector is kept in the list of managed connectors for as long as the underlying resource is available.

    Resources are added via the "resource_available()" method and removed via "resource_unavailable()`.

    Fires ConnectorConnectedEvent when a connector is available.
    Fires ConnectorDisconnectedEvent when the connector is disconnected.

    :param connected_loop  a callable that is regularly called while a connection is active.
    """

    def __init__(self, connected_loop=None, retry_period=5):
        """
        :param retry_period: how frequently (in seconds) to refresh connections that are not connected
        """
        self.retry_period = retry_period
        self._connections = dict()   # a map from resource to MaintainedConnection
        self.events = QueuedEventSource()
        self._connected_loop = connected_loop

    def unavailable(self, resource, connector: Connector=None):
        """register the given resource as being unavailable.
        It is removed from the managed connections."""
        if resource in self._connections:
            connection = self._connections[resource]
            connection.loop.stop()
            connection.loop = None  # free cyclic reference
            del self._connections[resource]

    def available(self, resource, connector: Connector):
        """ Notifies this manager that the given connector is available as a possible controller connection.
            :param: resource    A key identifying the resource
            :param: connector  A Connector instance that can connect to the resource endpoint
            If the resource is already connected to the given connector,
            the method returns quietly. Otherwise, the existing managed connection
            is stopped before being replaced with a new managed connection
            to the connector.
            """
        previous = self._connections.get(resource, None)
        if previous is not None:
            if previous.connector is connector:
                return
            else:
                previous.loop.stop()     # connector has changed
        conn = self._connections[resource] = self._new_maintained_connection(resource, connector,
                                                                             self.retry_period, self.events)
        conn.loop.start()

    def _new_maintained_connection(self, resource, connector, timeout, events):
        mc = MaintainedConnection(resource, connector, PeriodRetryStrategy(timeout), events)
        loop = MaintainedConnectionLoop(mc)
        mc.loop = loop
        return mc

    @property
    def connections(self):
        """
        retrieves a mapping from the resource key to the MaintainedConnection.
        Note that connections may or may not be connected.
        """
        return dict(self._connections)

    def maintain(self, current_time=time.time):
        """
        updates all managed connections on this manager.
        """
        for managed_connection in self.connections.values():
            try:
                managed_connection.maintain(current_time())
            except Exception as e:
                logger.exception("unexpected exception '%s' on '%s', closing." % (e, managed_connection))
                managed_connection.close()

    def update(self):
        self.events.publish()


class ControllerConnectionManager(ConnectionManager):
    """
    runs the controller protocol as part of the background thread loop
    """
    def __init__(self, retry_period=5):
        super().__init__(self._connected_loop, retry_period)

    def _connected_loop(self, maintained_connection):
        maintained_connection.connector.protocol.read_response_async()


class ConnectionDiscovery:
    """
    Listens for events from a ResourceDiscovery instance and uses the connector_factory to create a connector
    corresponding to the resource type discovered.

    The connector is left unopened, and used to notify
    a ConnectorManager about the resource availability.

    :param discovery:   a resource discovery that is polled from time to time
        to discover new resources.
    """
    def __init__(self, discovery: PolledResourceDiscovery, connector_factory,
                 connector_manager: ControllerConnectionManager=None):
        """
        :param discovery A ResourceDiscovery instance that publishes events as resources become available.
        :param connector_factory A callable. Given the (key,target) info from the ResourceDiscovery,
            the factory is responsible for creating a connector.
        :param connector_manager The manager that is notified of resources changing availability. Should support
            available(resource, connector) and unavailable(resource)
        """
        self.discovery = discovery
        self.connector_factory = connector_factory
        self.manager = connector_manager
        listeners = discovery.listeners
        listeners += self.resource_event

    def dispose(self):
        self.discovery.listeners.remove(self.resource_event)

    def _create_connector(self, resource):
        return self.connector_factory(resource)

    def resource_event(self, event):
        """ receives resource notifications from the ResourceDiscovery.
            When a resource is available, the connector factory is invoked to create a connector for the resource.
            When a resource is unavailable, the connection manager is notified.
        """
        if not self.manager:
            return
        if type(event) is ResourceAvailableEvent:
            connector = self._create_connector(event.resource)
            if connector:
                self.manager.available(event.key, connector)
        elif type(event) is ResourceUnavailableEvent:
            self.manager.unavailable(event.key)

    def update(self):
        """
        Updates discovered resources.
        """
        self.discovery.update()


class ControllerDiscoveryFacade:
    """
    A facade for listening to different types of connectible resources, such as serial ports, TCP servers, local
    program images.

    """
    default_serial_baudrate = 57600

    def __init__(self, controller_discoveries):
        """
        :param controller_discoveries  ControllerDiscovery instances used to detect endpoints.
            See build_serial_discovery and build_tcp_server_discovery
        """
        self.manager = ControllerConnectionManager()
        self.discoveries = controller_discoveries
        for d in self.discoveries:
            d.manager = self.manager

    def update(self):
        """
        updates all the discovery objects added to the facade.
        """
        for d in self.discoveries:
            d.update()
        self.manager.update()

    @staticmethod
    def default_serial_setup(serial: Serial):
        """ Applies the default serial setup for a serial connection to a controller. """
        serial.baudrate = ControllerDiscoveryFacade.default_serial_baudrate

    @staticmethod
    def build_serial_discovery(protocol_sniffer, setup_serial=None) ->ConnectionDiscovery:
        """
        Constructs a ControllerDiscovery instance suited to discovering serial controllers.
         :param protocol_sniffer:   A callable that takes a Conduit and is responsible for decoding the Protocol to use,
            or raise a UnknownProtocolError. See AbstractConnector.
         :param setup_serial    A callable that is passed a non-open Serial instance and allowed to modify the
            serial protocol (baud rate, stop bits, parity etc..)  The result from the callable is ignored.
        """
        discovery = SerialDiscovery()
        if setup_serial is None:
            setup_serial = ControllerDiscoveryFacade.default_serial_setup

        def connector_factory(resource):
            key = resource[0]
            serial = Serial()
            serial.port = key
            setup_serial(serial)
            connector = SerialConnector(serial)
            connector = CloseOnErrorConnector(connector)
            return ProtocolConnector(connector, protocol_sniffer)

        return ConnectionDiscovery(discovery, connector_factory)

    @staticmethod
    def build_tcp_server_discovery(protocol_sniffer, service_type, known_addresses):
        """
        Creates a ControllerDiscovery instance suited to discovering local server controllers.
        :param protocol_sniffer A callable that takes a Conduit and is responsible for decoding the
            protocol, or raise a UnknownProtocolError. See AbstractConnector
        :param service_type A string that identifies the specific type of TCP service. This is an application
            defined name.
        """
        discovery = TCPServerDiscovery(service_type, known_addresses=known_addresses)

        def connector_factory(resource: TCPServerEndpoint):
            connector = SocketConnector(sock_args=(), connect_args=(resource.hostname, resource.port),
                                        report_errors=False)
            connector = CloseOnErrorConnector(connector)
            return ProtocolConnector(connector, protocol_sniffer)

        return ConnectionDiscovery(discovery, connector_factory)

    @staticmethod
    def build_process_discovery(protocol_sniffer, file, args, cwd=None):
        """
        Creates a ControllerDiscovery instance suited to discovering local executable controllers.
        :param protocol_sniffer A callable that takes a Conduit and is responsible for decoding the
            protocol, or raise a UnknownProtocolError. See AbstractConnector
        :param file The filename of the process file to open.
        """
        discovery = ProcessDiscovery(file)

        def connector_factory(resource):
            connector = ProcessConnector(resource, args, cwd=cwd)
            connector = CloseOnErrorConnector(connector)
            return ProtocolConnector(connector, protocol_sniffer)

        return ConnectionDiscovery(discovery, connector_factory)
