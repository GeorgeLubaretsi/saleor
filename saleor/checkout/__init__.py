from collections import defaultdict

from django.conf import settings
from prices import Price
from satchless.process import ProcessManager
from satchless.item import Partitioner

from .steps import (BillingAddressStep, ShippingStep, DigitalDeliveryStep,
                    SummaryStep)
from ..cart import DigitalGroup, Cart
from ..core import analytics
from ..order.models import Order
from ..userprofile.models import Address

STORAGE_SESSION_KEY = 'checkout_storage'


class CheckoutStorage(defaultdict):

    modified = False

    def __init__(self, *args, **kwargs):
        super(CheckoutStorage, self).__init__(dict, *args, **kwargs)


class Checkout(ProcessManager):

    items = None
    groups = None
    billing = None
    steps = None

    def __init__(self, request):
        self.request = request
        self.groups = []
        self.steps = []
        self.items = []
        try:
            self.storage = CheckoutStorage(
                request.session[STORAGE_SESSION_KEY])
        except KeyError:
            self.storage = CheckoutStorage()
        self.cart = Cart.for_session_cart(request.cart)
        self.generate_steps(self.cart)

    def generate_steps(self, cart):
        self.items = Partitioner(cart)
        self.billing = BillingAddressStep(
            self.request, self.get_storage('billing'))
        self.steps.append(self.billing)
        for index, delivery_group in enumerate(self.items):
            if isinstance(delivery_group, DigitalGroup):
                storage = self.get_storage('digital_%s' % (index,))
                step = DigitalDeliveryStep(
                    self.request, storage, delivery_group, _id=index)
            else:
                storage = self.get_storage('shipping_%s' % (index,))
                step = ShippingStep(
                    self.request, storage, delivery_group, _id=index,
                    default_address=self.billing_address)
            self.steps.append(step)
        summary_step = SummaryStep(
            self.request, self.get_storage('summary'), checkout=self)
        self.steps.append(summary_step)

    @property
    def anonymous_user_email(self):
        storage = self.get_storage('billing')
        return storage.get('anonymous_user_email')

    @anonymous_user_email.setter
    def anonymous_user_email(self, email):
        storage = self.get_storage('billing')
        storage['anonymous_user_email'] = email

    @anonymous_user_email.deleter
    def anonymous_user_email(self):
        storage = self.get_storage('billing')
        storage['anonymous_user_email'] = ''

    @property
    def billing_address(self):
        storage = self.get_storage('billing')
        address_data = storage.get('address', {})
        return Address(**address_data)

    @billing_address.setter
    def billing_address(self, address):
        storage = self.get_storage('billing')
        storage['address'] = address.as_data()

    @billing_address.deleter
    def billing_address(self):
        storage = self.get_storage('billing')
        storage['address'] = None

    def get_storage(self, name):
        return self.storage[name]

    def get_total(self, **kwargs):
        zero = Price(0, currency=settings.DEFAULT_CURRENCY)
        total = sum((step.group.get_total_with_delivery(**kwargs)
                     for step in self if step.group),
                    zero)
        return total

    def save(self):
        self.request.session[STORAGE_SESSION_KEY] = dict(self.storage)

    def clear_storage(self):
        del self.request.session[STORAGE_SESSION_KEY]
        self.cart.clear()

    def __iter__(self):
        return iter(self.steps)

    def delivery_steps(self):
        return [step for step in self.steps if step.group]

    def create_order(self):
        order = Order()
        if self.request.user.is_authenticated():
            order.user = self.request.user
        for step in self.steps:
            step.add_to_order(order)
        if self.request.user.is_authenticated():
            order.anonymous_user_email = ''
        order.tracking_client_id = analytics.get_client_id(self.request)
        order.save()
        return order
