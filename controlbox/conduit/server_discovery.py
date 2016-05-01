from queue import Queue
from zeroconf import Zeroconf, ServiceBrowser
from controlbox.conduit.discovery import PolledResourceDiscovery, ResourceUnavailableEvent, ResourceAvailableEvent
from controlbox.connector.socketconn import TCPServerEndpoint
import logging

logger = logging.getLogger(__name__)


class ZeroconfTCPServerEndpoint(TCPServerEndpoint):
    """
    Creates a tcp endpoint from the info provided by a zeroconf-registered service.
    """
    def __init__(self, info):
        super().__init__(info.server, info.address, info.port)
        self.info = info


class TCPServerDiscovery(PolledResourceDiscovery):
    """
    Uses zeroconf to discover TCP services.
    To keep all the events on the same thread, this captures events from zeroconf and pushes
    them to a queue. These events are then posted next time update() is called.
    The resources discovered are ZeroconfTCPServerEndpoint.
    """
    def __init__(self, service_subtype, use_zeroconf=True):
        """
         :param service_subtype  The subtype of the TCP services to detect. This is an application-specific name.
            The type is qualified automatically with TCP and local supertypes.
            The subtype should not begin with an underscore, and does not need a separating "." at th eend.
        """
        super().__init__()
        self.event_queue = Queue()
        if use_zeroconf:
            self.zeroconf = Zeroconf()
            self.browser = ServiceBrowser(self.zeroconf, TCPServerDiscovery.qualify_service_type(service_subtype), self)
        else:
            self.zeroconf = None
            self.browser = None

    @staticmethod
    def qualify_service_type(service_subtype):
        """
        >>> TCPServerDiscovery.qualify_service_type("abc")
        "_abc._tcp._local."
        """
        return "_" + service_subtype + "._tcp._local."

    @staticmethod
    def resource_for_service(zeroconf, type, name):
        """
        constructs the ZeroconfTCPServerEndpoint from the zeroconf info
        """
        info = zeroconf.get_service_info(type, name)
        resource = None if not info else ZeroconfTCPServerEndpoint(info)
        return resource

    def _publish(self, event, zeroconf, svc_type, svc_name):
        """
        publishes an event corresponding to the given service. The event is published
        only if zeroconf provides info for the service name and type.
        """
        info = self.resource_for_service(zeroconf, svc_type, svc_name)
        if info:
            self.event_queue.put(event(self, info))
        else:
            logger.warn("no info for service %s type %s" % (svc_name, svc_type))

    def remove_service(self, zeroconf, type, name):
        """ notification from the service browser that a service has been removed """
        self._publish(ResourceUnavailableEvent, zeroconf, type, name)

    def add_service(self, zeroconf, type, name):
        """ notification from the service browser that a service has been added """
        self._publish(ResourceAvailableEvent, zeroconf, type, name)

    def update(self):
        queue = self.event_queue
        if not queue.empty():
            events = []
            while not queue.empty():
                events.append(queue.get())
            self._fire_events(events)