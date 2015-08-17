#
# Registration dialog/wizard
#
# Copyright (c) 2011 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

import gettext
import logging
import Queue
import re
import socket
import sys
import threading


from subscription_manager.ga import Gtk as ga_Gtk
from subscription_manager.ga import GObject as ga_GObject

import rhsm.config as config
from rhsm.utils import ServerUrlParseError
from rhsm.connection import GoneException

from subscription_manager.branding import get_branding
from subscription_manager.action_client import ActionClient
from subscription_manager.gui import networkConfig
from subscription_manager.gui import widgets
from subscription_manager.injection import IDENTITY, PLUGIN_MANAGER, require, \
        INSTALLED_PRODUCTS_MANAGER, PROFILE_MANAGER
from subscription_manager import managerlib
from subscription_manager.utils import is_valid_server_info, MissingCaCertException, \
        parse_server_info, restart_virt_who

from subscription_manager.gui.utils import handle_gui_exception, show_error_window
from subscription_manager.gui.autobind import DryRunResult, \
        ServiceLevelNotSupportedException, AllProductsCoveredException, \
        NoProductsException
from subscription_manager.gui.messageWindow import InfoDialog, OkDialog
from subscription_manager.jsonwrapper import PoolWrapper

_ = lambda x: gettext.ldgettext("rhsm", x)

gettext.textdomain("rhsm")

#Gtk.glade.bindtextdomain("rhsm")

#Gtk.glade.textdomain("rhsm")

log = logging.getLogger('rhsm-app.' + __name__)

CFG = config.initConfig()

REGISTERING = 0
SUBSCRIBING = 1
state = REGISTERING


def get_state():
    global state
    return state


def set_state(new_state):
    global state
    state = new_state

ERROR_SCREEN = -3
DONT_CHANGE = -2
PROGRESS_PAGE = -1
CHOOSE_SERVER_PAGE = 0
ACTIVATION_KEY_PAGE = 1
CREDENTIALS_PAGE = 2
OWNER_SELECT_PAGE = 3
ENVIRONMENT_SELECT_PAGE = 4
PERFORM_REGISTER_PAGE = 5
SELECT_SLA_PAGE = 6
CONFIRM_SUBS_PAGE = 7
PERFORM_SUBSCRIBE_PAGE = 8
REFRESH_SUBSCRIPTIONS_PAGE = 9
INFO_PAGE = 10
DONE_PAGE = 11
FINISH = 100

REGISTER_ERROR = _("<b>Unable to register the system.</b>") + \
    "\n%s\n" + \
    _("Please see /var/log/rhsm/rhsm.log for more information.")


# from old smolt code.. Force glibc to call res_init()
# to rest the resolv configuration, including reloading
# resolv.conf. This attempt to handle the case where we
# start up with no networking, fail name resolution calls,
# and cache them for the life of the process, even after
# the network starts up, and for dhcp, updates resolv.conf
def reset_resolver():
    """Attempt to reset the system hostname resolver.
    returns 0 on success, or -1 if an error occurs."""
    try:
        import ctypes
        try:
            resolv = ctypes.CDLL("libc.so.6")
            r = resolv.__res_init()
        except (OSError, AttributeError):
            log.warn("could not find __res_init in libc.so.6")
            r = -1
        return r
    except ImportError:
        # If ctypes isn't supported (older versions of python for example)
        # Then just don't do anything
        pass
    except Exception, e:
        log.warning("reset_resolver failed: %s", e)
        pass


class RegistrationBox(widgets.SubmanBaseWidget):
    gui_file = "registration_box"


class RegisterInfo(ga_GObject.GObject):
    #username = None
    consumername = None
    activation_keys = None
    owner_key = None
    current_sla = None
    dry_run_result = None
    skip_auto_bind = False

    username = ga_GObject.property(type=str, default='')
    password = ga_GObject.property(type=str, default='')
    hostname = ga_GObject.property(type=str, default='')
    port = ga_GObject.property(type=str, default='')
    prefix = ga_GObject.property(type=str, default='')
    environment = ga_GObject.property(type=str, default='')

    @property
    def identity(self):
        id = require(IDENTITY)
        return id

    def __init__(self):
        ga_GObject.GObject.__init__(self)


class RegisterWidget(widgets.SubmanBaseWidget):
    gui_file = "registration"
    widget_names = ['register_widget', 'register_notebook',
                    'register_details_label', 'register_progressbar',
                    'progress_label']

    __gsignals__ = {'proceed': (ga_GObject.SIGNAL_RUN_FIRST,
                                None, (int,)),
                    'register-error': (ga_GObject.SIGNAL_RUN_FIRST,
                              None, []),
                    'register-failure': (ga_GObject.SIGNAL_RUN_FIRST,
                               None, []),
                    'attach-error': (ga_GObject.SIGNAL_RUN_FIRST,
                              None, []),
                    'attach-failure': (ga_GObject.SIGNAL_RUN_FIRST,
                               None, []),
                    'finished': (ga_GObject.SIGNAL_RUN_FIRST,
                                 None, [])}

    details_label_txt = ga_GObject.property(type=str, default='')
    register_state = ga_GObject.property(type=int, default=REGISTERING)
    register_button_label = ga_GObject.property(type=str, default=_('Register'))

    def __init__(self, backend, facts, parent=None):
        super(RegisterWidget, self).__init__()

        log.debug("RegisterWidget")
        #widget
        self.backend = backend
        self.identity = require(IDENTITY)
        self.facts = facts

        # widget
        self.async = AsyncBackend(self.backend)

        self.parent = parent

        self.info = RegisterInfo()

        self.info.connect("notify::username", self._on_username_password_change)
        self.info.connect("notify::password", self._on_username_password_change)
        self.info.connect("notify::hostname", self._on_connection_info_change)
        self.info.connect("notify::port", self._on_connection_info_change)
        self.info.connect("notify::prefix", self._on_connection_info_change)

        # FIXME: change glade name
        self.details_label = self.register_details_label
        self.connect('notify::details-label-txt', self._on_details_label_txt_change)
        self.connect('notify::register-state', self._on_register_state_change)

        self.register_notebook.connect('switch-page', self._on_switch_page)

        #widget
        screen_classes = [ChooseServerScreen, ActivationKeyScreen,
                          CredentialsScreen, OrganizationScreen,
                          EnvironmentScreen, PerformRegisterScreen,
                          SelectSLAScreen, ConfirmSubscriptionsScreen,
                          PerformSubscribeScreen, RefreshSubscriptionsScreen,
                          InfoScreen, DoneScreen]
        self._screens = []

        for screen_class in screen_classes:
            screen = screen_class(parent=self)
            screen.connect('move-to-screen', self._on_move_to_screen)
            screen.connect('stay-on-screen', self._on_stay_on_screen)
            self._screens.append(screen)
            if screen.needs_gui:
                screen.index = self.register_notebook.append_page(
                        screen.container, tab_label=None)

        # TODO: current_screen as property?
        self._current_screen = CHOOSE_SERVER_PAGE

        self.username = None
        self.consumername = None
        self.activation_keys = None
        self.owner_key = None
        self.environment = None
        self.current_sla = None
        self.dry_run_result = None
        self.skip_auto_bind = False

        # FIXME: modify property instead
        self.callbacks = []

        self.register_widget.show()

    def initialize(self):
        log.debug("RegisterWidget.initialize")
        self.set_initial_screen()
        self.clear_screens()
        self.timer = ga_GObject.timeout_add(100, self._timeout_callback)
        self.register_widget.show_all()

    def set_initial_screen(self):
        target = self._get_initial_screen()
        self._set_screen(target)

    def _get_initial_screen(self):
        return CHOOSE_SERVER_PAGE

    # switch-page should be after the current screen is reset
    def _on_switch_page(self, notebook, page, page_num):
        log.debug("\n\n_on_switch_page, notebook=%s page=%s page_num=%s\n\n",
                  notebook, page, page_num)
        current_screen = self._screens[self._current_screen]
        self.set_property('register-button-label', current_screen.button_label)

    def _on_username_password_change(self, *args):
        log.debug("on_username_password_change args=%s", args)
        self.backend.cp_provider.set_user_pass(self.info.username, self.info.password)
        self.backend.update()

    def _on_connection_info_change(self, *args):
        log.debug("on_connection_info_change args=%s", args)
        self.backend.update()

    def _on_stay_on_screen(self, current_screen):
        self._set_screen(self._current_screen)

    # TODO: replace most of the gui flow logic in the Screen subclasses with
    #       some state machine that drives them, possibly driving via signals
    #       indicating each state

    def _on_move_to_screen(self, current_screen, next_screen_id):
        log.debug("_on_move_to_screen current_screen_id=%s next_screen_id=%s",
                  current_screen, next_screen_id)
        # run current_screen post()
        # point to next_screen
        # run next screens pre()
        # if next_screen.pre() is async,
        #    go progress screen

        # FIXME: I think we can ditch post()
        # Split into handling current screen post (the 'out') and
        # the next screens pre() (the 'in')
        current_screen.post()

        self.change_screen(next_screen_id)

    def change_screen(self, next_screen_id):
        next_screen = self._screens[next_screen_id]
        self._set_screen(next_screen_id)

        async = next_screen.pre()
        if async:
            next_screen.emit('move-to-screen', PROGRESS_PAGE)

    def _set_screen(self, screen):
        log.debug("registerWidget._set_screen _current_screen=%s screen=%s", self._current_screen, screen)
        if screen > PROGRESS_PAGE:
            self._current_screen = screen
            # FIXME: If we just add ProgressPage in the screen order, we
            # shouldn't need this bookeeping
            if self._screens[screen].needs_gui:
                self.register_notebook.set_current_page(self._screens[screen].index)
        else:
            self.register_notebook.set_current_page(screen + 1)

    # FIXME: figure out to determine we are on first screen, then this
    # could just be 'move-to-screen', next screen
    # Go to the next screen/state
    def do_proceed(self, args):
        log.debug("registerWidget.proceed")
        log.debug("do_proceed args=%s", args)
        result = self._screens[self._current_screen].apply()
        log.debug("current screen.apply result=%s", result)

    def finish_registration(self):
        ga_GObject.source_remove(self.timer)

        self.register_finished()

    # for subman gui, we don't need to switch screens on error
    # but for firstboot, we will go back to the info screen if
    # we have it.
    def register_error_screen(self):
        log.debug("register_error_screen")
        self._set_screen(CHOOSE_SERVER_PAGE)

    def attach_error_screen(self):
        log.debug("attach_error_screen")
        # FIXME: maybe need a usually skipped "about to attach" screen?
        self._set_screen(SELECT_SLA_PAGE)

    # Error raised by a notebook page/screen
    def screen_error(self):
        self.emit('error')

    def register_finished(self):
        self.emit('finished')

    # when we decide we can not complete registration
    def register_failure(self):
        self.emit('register-failure')

    # when we hit a recoverable error during registration
    def register_error(self):
        self.emit('register-error')

    # if we registered, but auto attach can not complete
    def attach_failure(self):
        self.emit('attach-failure')

    # we've hit a recoverable error during auto-attach
    def attach_error(self):
        self.emit('attach-error')

    def _on_details_label_txt_change(self, obj, value):
        self.details_label.set_label("<small>%s</small>" %
                                     self.get_property('details-label-txt'))

    def _on_register_state_change(self, obj, value):
        state = self.get_property('register-state')
        if state == REGISTERING:
            self.progress_label.set_markup(_("<b>Registering</b>"))
        elif state == SUBSCRIBING:
            self.progress_label.set_markup(_("<b>Attaching</b>"))

    def clear_screens(self):
        for screen in self._screens:
            screen.clear()

    def _timeout_callback(self):
        self.register_progressbar.pulse()
        # return true to keep it pulsing
        return True


class RegisterDialog(widgets.SubmanBaseWidget):

    widget_names = ['register_dialog', 'register_dialog_main_vbox',
                    'register_progressbar', 'register_details_label',
                    'cancel_button', 'register_button', 'progress_label',
                    'dialog_vbox6']
    gui_file = "register_dialog"
    __gtype_name__ = 'RegisterDialog'

    def __init__(self, backend, facts=None, callbacks=None):
        """
        Callbacks will be executed when registration status changes.
        """
        super(RegisterDialog, self).__init__()

        #dialog
        callbacks = {"on_register_cancel_button_clicked": self.cancel,
                     "on_register_button_clicked": self._on_register_button_clicked,
                     "hide": self.cancel,
                     "on_register_dialog_delete_event": self.cancel}
        self.connect_signals(callbacks)

        # FIXME: Need better error handling in general, but it's kind of
        # annoying to have to pass the top level widget all over the place
        self.register_widget = RegisterWidget(backend, facts, parent=self.register_dialog)
        # Ensure that we start on the first page and that
        # all widgets are cleared.
        self.register_widget.set_initial_screen()
        self.register_widget.initialize()

        self.register_dialog_main_vbox.pack_start(self.register_widget.register_widget,
                                                  True, True, 0)

        self.register_button.connect('clicked', self._on_register_button_clicked)
        self.cancel_button.connect('clicked', self.cancel)

        self.register_widget.connect('finished', self.cancel)
        self.register_widget.connect('register-error', self.on_register_error)
        self.register_widget.connect('register-failure', self.on_register_failure)
        self.register_widget.connect('attach-error', self.on_attach_error)
        self.register_widget.connect('attach-failure', self.on_attach_failure)

        # update window title on register state changes
        self.register_widget.connect('notify::register-state',
                                     self._on_register_state_change)

        # update the 'next/register button on page change'
        self.register_widget.connect('notify::register-button-label',
                                       self._on_register_button_label_change)

        # initial-setup wants a attr named 'window'
        self.window = self.register_dialog

        # XXX needed by firstboot
        self.password = None

    def initialize(self):
        self.register_widget.clear_screens()
        self.register_widget.initialize()
        log.debug("RegisterScreen.initialize")

    def show(self):
        # initial-setup module skips this, since it results in a
        # new top level window that isn't reparented to the initial-setup
        # screen.

        self.register_dialog.show()
        log.debug("RegsiterScreen.show")

    def cancel(self, button):
        self.register_dialog.hide()
        return True

    def on_register_error(self, args):
        log.debug("register_dialog.on_register_error args=%s", args)
        # FIXME: can we just ignore this for sm-gui?
        self.register_widget.register_error_screen()

    def on_register_failure(self, args):
        log.debug("register_dialog.on_register_failure args=%s", args)
        self.register_dialog.hide()

    def on_attach_error(self, args):
        log.debug("register_dialog.on_attach_error args=%s", args)
        # FIXME: can we just ignore this for sm-gui?
        self.register_widget.attach_error_screen()

    def on_attach_failure(self, args):
        log.debug("register_dialog.on_attach_failure args=%s", args)
        self.register_dialog.hide()

    def _on_register_button_clicked(self, button):
        log.debug("dialog on_register_button_clicked, button=%s, %s", button, self.register_widget)
        self.register_widget.emit('proceed', 42)
        log.debug("post")

    def _on_register_state_change(self, obj, value):
        state = obj.get_property('register-state')
        if state == REGISTERING:
            self.register_dialog.set_title(_("System Registration"))
        elif state == SUBSCRIBING:
            self.register_dialog.set_title(_("Subscription Attachment"))

    def _on_register_button_label_change(self, obj, value):
        register_label = obj.get_property('register-button-label')
        self.register_button.set_label(register_label)


class AutobindWizard(RegisterDialog):

    def __init__(self, backend, facts, parent):
        super(AutobindWizard, self).__init__(backend, facts, parent)

    def show(self):
        super(AutobindWizard, self).show()
        self.register_widget.change_screen(SELECT_SLA_PAGE)

    def _get_initial_screen(self):
        return SELECT_SLA_PAGE


class Screen(widgets.SubmanBaseWidget):
    widget_names = ['container']
    gui_file = None

    # TODO: replace page int with class enum
    __gsignals__ = {'stay-on-screen': (ga_GObject.SIGNAL_RUN_FIRST,
                                 None, []),
                    'move-to-screen': (ga_GObject.SIGNAL_RUN_FIRST,
                                     None, (int,))}

    def __init__(self, parent):
        super(Screen, self).__init__()
        log.debug("Screen %s init parent=%s", self.__class__.__name__, parent)

        self.pre_message = ""
        self.button_label = _("Register")
        self.needs_gui = True
        self.index = -1
        self._parent = parent
        self._error_screen = self.index

    def stay(self):
        self.emit('stay-on-screen')
        #self.emit('move-to-screen', DONT_CHANGE)

    def pre(self):
        return False

    def apply(self):
        pass

    def post(self):
        pass

    def clear(self):
        pass


class NoGuiScreen(ga_GObject.GObject):

    __gsignals__ = {'identity-updated': (ga_GObject.SIGNAL_RUN_FIRST,
                                         None, []),
                    'move-to-screen': (ga_GObject.SIGNAL_RUN_FIRST,
                                       None, (int,)),
                    'stay-on-screen': (ga_GObject.SIGNAL_RUN_FIRST,
                                       None, []),
                    'certs-updated': (ga_GObject.SIGNAL_RUN_FIRST,
                                      None, [])}

    def __init__(self, parent):
        ga_GObject.GObject.__init__(self)

        self._parent = parent
        self.button_label = None
        self.needs_gui = False
        self._error_screen = None
        self.pre_message = "Default Pre Message"

    def pre(self):
        return True

    def apply(self):
        self.emit('move-to-screen', 1)

    def post(self):
        pass

    def clear(self):
        pass


class ProgressScreen(NoGuiScreen):
    def pre(self):
        pass


class PerformRegisterScreen(NoGuiScreen):

    def __init__(self, parent):
        super(PerformRegisterScreen, self).__init__(parent)
        self._error_screen = CREDENTIALS_PAGE

    def _on_registration_finished_cb(self, new_account, error=None):
        if error is not None:
            handle_gui_exception(error, REGISTER_ERROR, self._parent.parent)
            self._parent.register_error()
            return

        try:
            managerlib.persist_consumer_cert(new_account)
            # FIXME: property/signal
            #self._backend.cs.force_cert_check()  # Ensure there isn't much wait time
            self.emit('identity-updated')

            if self._parent.activation_keys:
                self.emit('move-to-screen', REFRESH_SUBSCRIPTIONS_PAGE)
            elif self._parent.skip_auto_bind:
                self._parent.finish_registration()
            else:
                self.emit('move-to-screen', SELECT_SLA_PAGE)
        except Exception, e:
            handle_gui_exception(e, REGISTER_ERROR, self._parent.parent)
            self._parent.register_error()

    def pre(self):
        log.info("Registering to owner: %s environment: %s" %
                 (self._parent.owner_key, self._parent.environment))

        self._parent.async.register_consumer(self._parent.consumername,
                                             self._parent.facts,
                                             self._parent.owner_key,
                                             self._parent.environment,
                                             self._parent.activation_keys,
                                             self._on_registration_finished_cb)

        return True


class PerformSubscribeScreen(NoGuiScreen):

    def __init__(self, parent):
        super(PerformSubscribeScreen, self).__init__(parent)
        self.pre_message = _("Attaching subscriptions")

    def _on_subscribing_finished_cb(self, unused, error=None):
        log.debug("_on_subscribing_finished_cb error=%s", error)
        if error is not None:
            handle_gui_exception(error, _("Error subscribing: %s"),
                                 self._parent.parent)
            self._parent.attach_error()
            return

        self.emit('certs-updated')
        self._parent.finish_registration()

    def pre(self):
        self._parent.set_property('details-label-txt', self.pre_message)
        self._parent.async.subscribe(self._parent.identity.uuid,
                                     self._parent.current_sla,
                                     self._parent.dry_run_result,
                                     self._on_subscribing_finished_cb)

        return True


class ConfirmSubscriptionsScreen(Screen):
    """ Confirm Subscriptions GUI Window """

    widget_names = Screen.widget_names + ['subs_treeview', 'back_button',
                                          'sla_label']

    gui_file = "confirmsubs"

    def __init__(self, parent):

        super(ConfirmSubscriptionsScreen, self).__init__(parent)
        self.button_label = _("Attach")

        self.store = ga_Gtk.ListStore(str, bool, str)
        self.subs_treeview.set_model(self.store)
        self.subs_treeview.get_selection().set_mode(ga_Gtk.SelectionMode.NONE)

        self.add_text_column(_("Subscription"), 0, True)

        column = widgets.MachineTypeColumn(1)
        column.set_sort_column_id(1)
        self.subs_treeview.append_column(column)

        self.add_text_column(_("Quantity"), 2)

    def add_text_column(self, name, index, expand=False):
        text_renderer = ga_Gtk.CellRendererText()
        column = ga_Gtk.TreeViewColumn(name, text_renderer, text=index)
        column.set_expand(expand)

        self.subs_treeview.append_column(column)
        column.set_sort_column_id(index)
        return column

    def apply(self):
        self.emit('move-to-screen', PERFORM_SUBSCRIBE_PAGE)

    def set_model(self):
        self._dry_run_result = self._parent.dry_run_result

        # Make sure that the store is cleared each time
        # the data is loaded into the screen.
        self.store.clear()
        self.sla_label.set_markup("<b>" + self._dry_run_result.service_level +
                                  "</b>")

        for pool_quantity in self._dry_run_result.json:
            self.store.append([pool_quantity['pool']['productName'],
                              PoolWrapper(pool_quantity['pool']).is_virt_only(),
                              str(pool_quantity['quantity'])])

    def pre(self):
        self.set_model()
        return False


class SelectSLAScreen(Screen):
    """
    An wizard screen that displays the available
    SLAs that are provided by the installed products.
    """
    widget_names = Screen.widget_names + ['product_list_label',
                                          'sla_radio_container',
                                          'owner_treeview']
    gui_file = "selectsla"

    def __init__(self, parent):
        super(SelectSLAScreen, self).__init__(parent)

        self.pre_message = _("Finding suitable service levels")
        self.button_label = _("Next")

        self._dry_run_result = None

    def set_model(self, unentitled_prod_certs, sla_data_map):
        self.product_list_label.set_text(
                self._format_prods(unentitled_prod_certs))
        group = None
        # reverse iterate the list as that will most likely put 'None' last.
        # then pack_start so we don't end up with radio buttons at the bottom
        # of the screen.
        for sla in reversed(sla_data_map.keys()):
            radio = ga_Gtk.RadioButton(group=group, label=sla)
            radio.connect("toggled", self._radio_clicked, sla)
            self.sla_radio_container.pack_start(radio, expand=False,
                                                fill=False, padding=0)
            radio.show()
            group = radio

        # set the initial radio button as default selection.
        group.set_active(True)

    def apply(self):
        self.emit('move-to-screen', CONFIRM_SUBS_PAGE)

    def post(self):
        self._parent.dry_run_result = self._dry_run_result

    def clear(self):
        child_widgets = self.sla_radio_container.get_children()
        for child in child_widgets:
            self.sla_radio_container.remove(child)

    def _radio_clicked(self, button, service_level):
        if button.get_active():
            self._dry_run_result = self._sla_data_map[service_level]

    def _format_prods(self, prod_certs):
        prod_str = ""
        for i, cert in enumerate(prod_certs):
            log.debug(cert)
            prod_str = "%s%s" % (prod_str, cert.products[0].name)
            if i + 1 < len(prod_certs):
                prod_str += ", "
        return prod_str

    # so much for service level simplifying things
    def _on_get_service_levels_cb(self, result, error=None):
        # The parent for the dialogs is set to the grandparent window
        # (which is MainWindow) because the parent window is closed
        # by finish_registration() after displaying the dialogs.  See
        # BZ #855762.
        log.debug("_on_get_service_levels_cb result=%s error=%s",
                  result, error)
        if error is not None:
            if isinstance(error[1], ServiceLevelNotSupportedException):
                OkDialog(_("Unable to auto-attach, server does not support service levels."),
                        parent=self._parent.parent)
                # FIXME: is this a failure or a finish?
            elif isinstance(error[1], NoProductsException):
                InfoDialog(_("No installed products on system. No need to attach subscriptions at this time."),
                           parent=self._parent.parent)
                # we are finished, close the register window
                self._parent.register_finished()
            elif isinstance(error[1], AllProductsCoveredException):
                InfoDialog(_("All installed products are covered by valid entitlements. No need to attach subscriptions at this time."),
                           parent=self._parent.parent)
                # We are finished, close the register window
                self._parent.register_finished()
            elif isinstance(error[1], GoneException):
                InfoDialog(_("Consumer has been deleted."), parent=self._parent.parent)
            else:
                log.exception(error)
                handle_gui_exception(error, _("Error subscribing"),
                                     self._parent.parent)
            # Assume this is a recoverable error
            self._parent.attach_error()
            return

        (current_sla, unentitled_products, sla_data_map) = result

        self._parent.current_sla = current_sla
        log.debug("current_sla=%s", current_sla)
        log.debug("unentitled_products=%s", unentitled_products)
        log.debug("sla_data_map=%s", sla_data_map)
        if len(sla_data_map) == 1:
            # If system already had a service level, we can hit this point
            # when we cannot fix any unentitled products:
            if current_sla is not None and \
                    not self._can_add_more_subs(current_sla, sla_data_map):
                handle_gui_exception(None,
                                     _("No available subscriptions at "
                                     "the current service level: %s. "
                                     "Please use the \"All Available "
                                     "Subscriptions\" tab to manually "
                                     "attach subscriptions.") % current_sla,
                                    self._parent.parent)
                self._parent.attach_failure()
                return

            self._dry_run_result = sla_data_map.values()[0]
            self.emit('move-to-screen', CONFIRM_SUBS_PAGE)
        elif len(sla_data_map) > 1:
            self._sla_data_map = sla_data_map
            self.set_model(unentitled_products, sla_data_map)
            self.stay()
            return
        else:
            log.info("No suitable service levels found.")
            handle_gui_exception(None,
                                 _("No service level will cover all "
                                 "installed products. Please manually "
                                 "subscribe using multiple service levels "
                                 "via the \"All Available Subscriptions\" "
                                 "tab or purchase additional subscriptions."),
                                 parent=self._parent.parent)
            log.debug("gh, no suitable sla post hge %s", self._parent)
            self._parent.attach_failure()

    def pre(self):
        self._parent.set_property('details-label-txt', self.pre_message)
        self._parent.set_property('register-state', SUBSCRIBING)
        #set_state(SUBSCRIBING)
        self._parent.identity.reload()
        self._parent.async.find_service_levels(self._parent.identity.uuid,
                                               self._parent.facts,
                                               self._on_get_service_levels_cb)
        return True

    def _can_add_more_subs(self, current_sla, sla_data_map):
        """
        Check if a system that already has a selected sla can get more
        entitlements at their sla level
        """
        if current_sla is not None:
            result = sla_data_map[current_sla]
            return len(result.json) > 0
        return False


class EnvironmentScreen(Screen):
    widget_names = Screen.widget_names + ['environment_treeview']
    gui_file = "environment"

    def __init__(self, parent):
        super(EnvironmentScreen, self).__init__(parent)

        self.pre_message = _("Fetching list of possible environments")
        renderer = ga_Gtk.CellRendererText()
        column = ga_Gtk.TreeViewColumn(_("Environment"), renderer, text=1)
        self.environment_treeview.set_property("headers-visible", False)
        self.environment_treeview.append_column(column)

    def _on_get_environment_list_cb(self, result_tuple, error=None):
        environments = result_tuple
        if error is not None:
            handle_gui_exception(error, REGISTER_ERROR, self._parent.parent)
            self._parent.register_error()
            return

        if not environments:
            self.set_environment(None)
            self.emit('move-to-screen', PERFORM_REGISTER_PAGE)
            return

        envs = [(env['id'], env['name']) for env in environments]
        if len(envs) == 1:
            self.set_environement(envs[0][0])
            self.emit('move-to-screen', PERFORM_REGISTER_PAGE)
        else:
            self.set_model(envs)
            self.stay()

            # TESTTHIS
            # self._parent.pre_done(DONT_CHANGE)

    def pre(self):
        self._parent.set_property('details-label-txt', self.pre_message)
        self._parent.async.get_environment_list(self._parent.owner_key,
                                                self._on_get_environment_list_cb)
        return True

    def apply(self):
        model, tree_iter = self.environment_treeview.get_selection().get_selected()
        self.set_environment(model.get_value(tree_iter, 0))
        self.emit('move-to-screen', PERFORM_REGISTER_PAGE)

    def set_environment(self, environment):
        log.debug("EnvScreen.set_environment %s", environment)
        self._parent.environment = environment
        self._parent.info.environment = environment

    def post(self):
        log.debug("ENV.POST _parent.env=%s _p.i.env=%s",
                  self._parent.environment, self._parent.info.environment)
        #self._parent.environment = self._environment

    def set_model(self, envs):
        environment_model = ga_Gtk.ListStore(str, str)
        for env in envs:
            environment_model.append(env)

        self.environment_treeview.set_model(environment_model)

        self.environment_treeview.get_selection().select_iter(
                environment_model.get_iter_first())


class OrganizationScreen(Screen):
    widget_names = Screen.widget_names + ['owner_treeview']
    gui_file = "organization"

    def __init__(self, parent):
        super(OrganizationScreen, self).__init__(parent)

        self.pre_message = _("Fetching list of possible organizations")

        renderer = ga_Gtk.CellRendererText()
        column = ga_Gtk.TreeViewColumn(_("Organization"), renderer, text=1)
        self.owner_treeview.set_property("headers-visible", False)
        self.owner_treeview.append_column(column)

        self._owner_key = None

    def _on_get_owner_list_cb(self, owners, error=None):
        if error is not None:
            handle_gui_exception(error, REGISTER_ERROR,
                    self._parent.parent)
            self._parent.register_error()
            return

        owners = [(owner['key'], owner['displayName']) for owner in owners]
        # Sort by display name so the list doesn't randomly change.
        owners = sorted(owners, key=lambda item: item[1])

        if len(owners) == 0:
            handle_gui_exception(None,
                                 _("<b>User %s is not able to register with any orgs.</b>") %
                                   (self._parent.username),
                    self._parent.window)
            self._parent.register_error()
            return

        if len(owners) == 1:
            self._owner_key = owners[0][0]
            # only one org, use it and skip the org selection screen
            #self._parent.pre_done(ENVIRONMENT_SELECT_PAGE)
            self.emit('move-to-screen', ENVIRONMENT_SELECT_PAGE)
        else:
            self.set_model(owners)
            # FIXME: we should be able to just not do anything here
            # TESTHIS
            self.stay()
            #self._parent.pre_done(DONT_CHANGE)

    def pre(self):
        self._parent.set_property('details-label-txt', self.pre_message)
        self._parent.async.get_owner_list(self._parent.username,
                                          self._on_get_owner_list_cb)
        return True

    def apply(self):
        # check for selection exists
        model, tree_iter = self.owner_treeview.get_selection().get_selected()
        self._owner_key = model.get_value(tree_iter, 0)
        self.emit('move-to-screen', ENVIRONMENT_SELECT_PAGE)

    def post(self):
        self._parent.owner_key = self._owner_key

    def set_model(self, owners):
        owner_model = ga_Gtk.ListStore(str, str)
        for owner in owners:
            owner_model.append(owner)

        self.owner_treeview.set_model(owner_model)

        self.owner_treeview.get_selection().select_iter(
                owner_model.get_iter_first())


class CredentialsScreen(Screen):
    widget_names = Screen.widget_names + ['skip_auto_bind', 'consumer_name',
                                          'account_login', 'account_password',
                                          'registration_tip_label',
                                          'registration_header_label']

    gui_file = "credentials"

    def __init__(self, parent):
        super(CredentialsScreen, self).__init__(parent)


        self._initialize_consumer_name()
        self.registration_tip_label.set_label("<small>%s</small>" %
                                          get_branding().GUI_FORGOT_LOGIN_TIP)

        self.registration_header_label.set_label("<b>%s</b>" %
                                             get_branding().GUI_REGISTRATION_HEADER)

    def _initialize_consumer_name(self):
        if not self.consumer_name.get_text():
            self.consumer_name.set_text(socket.gethostname())

    def _validate_consumername(self, consumername):
        if not consumername:
            show_error_window(_("You must enter a system name."), self._parent.window)
            self.consumer_name.grab_focus()
            return False
        return True

    def _validate_account(self):
        # validate / check user name
        if self.account_login.get_text().strip() == "":
            show_error_window(_("You must enter a login."), self._parent.window)
            self.account_login.grab_focus()
            return False

        if self.account_password.get_text().strip() == "":
            show_error_window(_("You must enter a password."), self._parent.window)
            self.account_password.grab_focus()
            return False
        return True

    def pre(self):
        self._parent.set_property('details-label-txt', self.pre_message)
        self.account_login.grab_focus()
        return False

    def apply(self):
        self._username = self.account_login.get_text().strip()
        self._password = self.account_password.get_text().strip()
        self._consumername = self.consumer_name.get_text()
        self._skip_auto_bind = self.skip_auto_bind.get_active()

        if not self._validate_consumername(self._consumername):
            #self.emit('move-to-screen', DONT_CHANGE)
            self.stay()
            return

        if not self._validate_account():
            self.stay()
            #self.emit('move-to-screen', DONT_CHANGE)
            return

        self._parent.info.username = self._username
        self._parent.info.password = self._password

        self.emit('move-to-screen', OWNER_SELECT_PAGE)

    def post(self):
        self._parent.username = self._username
        self._parent.password = self._password
        self._parent.consumername = self._consumername
        self._parent.skip_auto_bind = self._skip_auto_bind
        self._parent.activation_keys = None

    def clear(self):
        self.account_login.set_text("")
        self.account_password.set_text("")
        self.consumer_name.set_text("")
        self._initialize_consumer_name()
        self.skip_auto_bind.set_active(False)


class ActivationKeyScreen(Screen):
    widget_names = Screen.widget_names + [
                'activation_key_entry',
                'organization_entry',
                'consumer_entry',
        ]
    gui_file = "activation_key"

    def __init__(self, parent):
        super(ActivationKeyScreen, self).__init__(parent)
        self._initialize_consumer_name()

    def _initialize_consumer_name(self):
        if not self.consumer_entry.get_text():
            self.consumer_entry.set_text(socket.gethostname())

    def apply(self):
        self._activation_keys = self._split_activation_keys(
            self.activation_key_entry.get_text().strip())
        self._owner_key = self.organization_entry.get_text().strip()
        self._consumername = self.consumer_entry.get_text().strip()

        if not self._validate_owner_key(self._owner_key):
            #self.emit('move-to-screen', DONT_CHANGE)
            self.stay()
            return

        if not self._validate_activation_keys(self._activation_keys):
            #self.emit('move-to-screen', DONT_CHANGE)
            self.stay()
            return

        if not self._validate_consumername(self._consumername):
            #self.emit('move-to-screen', DONT_CHANGE)
            self.stay()
            return

        self.emit('move-to-screen', PERFORM_REGISTER_PAGE)

    def _split_activation_keys(self, entry):
        keys = re.split(',\s*|\s+', entry)
        return [x for x in keys if x]

    def _validate_owner_key(self, owner_key):
        if not owner_key:
            show_error_window(_("You must enter an organization."), self._parent.window)
            self.organization_entry.grab_focus()
            return False
        return True

    def _validate_activation_keys(self, activation_keys):
        if not activation_keys:
            show_error_window(_("You must enter an activation key."), self._parent.window)
            self.activation_key_entry.grab_focus()
            return False
        return True

    def _validate_consumername(self, consumername):
        if not consumername:
            show_error_window(_("You must enter a system name."), self._parent.window)
            self.consumer_entry.grab_focus()
            return False
        return True

    def pre(self):
        self._parent.set_property('details-label-txt', self.pre_message)
        self.organization_entry.grab_focus()
        return False

    def post(self):
        self._parent.activation_keys = self._activation_keys
        self._parent.owner_key = self._owner_key
        self._parent.consumername = self._consumername
        # Environments aren't used with activation keys so clear any
        # cached value.
        self._parent.environment = None
        self._backend.cp_provider.set_user_pass()


class RefreshSubscriptionsScreen(NoGuiScreen):

    def __init__(self, parent):
        super(RefreshSubscriptionsScreen, self).__init__(parent)
        self.pre_message = _("Attaching subscriptions")

    def _on_refresh_cb(self, error=None):
        if error is not None:
            handle_gui_exception(error, _("Error subscribing: %s"),
                                 self._parent.parent)
            self._parent.register_error()
            return

        self._parent.finish_registration()

    def pre(self):
        self._parent.set_property('details-label-txt', self.pre_message)
        self._parent.async.refresh(self._on_refresh_cb)
        return True


class ChooseServerScreen(Screen):
    widget_names = Screen.widget_names + ['server_entry', 'proxy_frame',
                                          'default_button', 'choose_server_label',
                                          'activation_key_checkbox']
    gui_file = "choose_server"

    def __init__(self, parent):

        super(ChooseServerScreen, self).__init__(parent)

        self.button_label = _("Next")

        callbacks = {
                "on_default_button_clicked": self._on_default_button_clicked,
                "on_proxy_button_clicked": self._on_proxy_button_clicked,
                "on_server_entry_changed": self._on_server_entry_changed,
            }

        self.connect_signals(callbacks)

        self.network_config_dialog = networkConfig.NetworkConfigDialog()

    def _on_default_button_clicked(self, widget):
        # Default port and prefix are fine, so we can be concise and just
        # put the hostname for RHN:
        self.server_entry.set_text(config.DEFAULT_HOSTNAME)

    def _on_proxy_button_clicked(self, widget):
        # proxy dialog may attempt to resolve proxy and server names, so
        # bump the resolver as well.
        self.reset_resolver()

        self.network_config_dialog.set_parent_window(self._parent.window)
        self.network_config_dialog.show()

    def _on_server_entry_changed(self, widget):
        """
        Disable the activation key checkbox if the user is registering
        to hosted.
        """
        server = self.server_entry.get_text()
        try:
            (hostname, port, prefix) = parse_server_info(server)
            if re.search('subscription\.rhn\.(.*\.)*redhat\.com', hostname):
                sensitive = False
                self.activation_key_checkbox.set_active(False)
            else:
                sensitive = True
            self.activation_key_checkbox.set_sensitive(sensitive)
        except ServerUrlParseError:
            # This may seem like it should be False, but we don't want
            # the checkbox blinking on and off as the user types a value
            # that is first unparseable and then later parseable.
            self.activation_key_checkbox.set_sensitive(True)

    def reset_resolver(self):
        try:
            reset_resolver()
        except Exception, e:
            log.warn("Error from reset_resolver: %s", e)

    def apply(self):
        server = self.server_entry.get_text()
        try:
            (hostname, port, prefix) = parse_server_info(server)
            CFG.set('server', 'hostname', hostname)
            CFG.set('server', 'port', port)
            CFG.set('server', 'prefix', prefix)

            self.reset_resolver()

            try:
                if not is_valid_server_info(hostname, port, prefix):
                    show_error_window(_("Unable to reach the server at %s:%s%s") %
                                      (hostname, port, prefix),
                                      self._parent.window)
                    self.emit('register-error')
                    return
            except MissingCaCertException:
                show_error_window(_("CA certificate for subscription service has not been installed."),
                                  self._parent.window)
                self.emit('register-error')
                return

        except ServerUrlParseError:
            show_error_window(_("Please provide a hostname with optional port and/or prefix: hostname[:port][/prefix]"),
                              self._parent.window)
            self.emit('register-error')
            return

        log.debug("Writing server data to rhsm.conf")
        CFG.save()

        self._parent.info.hostname = hostname
        self._parent.info.port = port
        self._parent.info.prefix = prefix

        if self.activation_key_checkbox.get_active():
            self.emit('move-to-screen', ACTIVATION_KEY_PAGE)
        else:
            self.emit('move-to-screen', CREDENTIALS_PAGE)

    def clear(self):
        # Load the current server values from rhsm.conf:
        current_hostname = CFG.get('server', 'hostname')
        current_port = CFG.get('server', 'port')
        current_prefix = CFG.get('server', 'prefix')

        # No need to show port and prefix for hosted:
        if current_hostname == config.DEFAULT_HOSTNAME:
            self.server_entry.set_text(config.DEFAULT_HOSTNAME)
        else:
            self.server_entry.set_text("%s:%s%s" % (current_hostname,
                    current_port, current_prefix))


class AsyncBackend(object):

    def __init__(self, backend):
        self.backend = backend
        self.plugin_manager = require(PLUGIN_MANAGER)
        self.queue = Queue.Queue()

    def _get_owner_list(self, username, callback):
        """
        method run in the worker thread.
        """
        try:
            retval = self.backend.cp_provider.get_basic_auth_cp().getOwnerList(username)
            self.queue.put((callback, retval, None))
        except Exception:
            self.queue.put((callback, None, sys.exc_info()))

    def _get_environment_list(self, owner_key, callback):
        """
        method run in the worker thread.
        """
        try:
            retval = None
            # If environments aren't supported, don't bother trying to list:
            if self.backend.cp_provider.get_basic_auth_cp().supports_resource('environments'):
                log.info("Server supports environments, checking for "
                         "environment to register with.")
                retval = []
                for env in self.backend.cp_provider.get_basic_auth_cp().getEnvironmentList(owner_key):
                    retval.append(env)
                if len(retval) == 0:
                    raise Exception(_("Server supports environments, but "
                        "none are available."))

            self.queue.put((callback, retval, None))
        except Exception:
            self.queue.put((callback, None, sys.exc_info()))

    def _register_consumer(self, name, facts, owner, env, activation_keys, callback):
        """
        method run in the worker thread.
        """
        try:
            installed_mgr = require(INSTALLED_PRODUCTS_MANAGER)

            self.plugin_manager.run("pre_register_consumer", name=name,
                facts=facts.get_facts())
            retval = self.backend.cp_provider.get_basic_auth_cp().registerConsumer(name=name,
                    facts=facts.get_facts(), owner=owner, environment=env,
                    keys=activation_keys,
                    installed_products=installed_mgr.format_for_server())
            self.plugin_manager.run("post_register_consumer", consumer=retval,
                facts=facts.get_facts())

            require(IDENTITY).reload()
            # Facts and installed products went out with the registration
            # request, manually write caches to disk:
            facts.write_cache()
            installed_mgr.write_cache()

            cp = self.backend.cp_provider.get_basic_auth_cp()

            # In practice, the only time this condition should be true is
            # when we are working with activation keys.  See BZ #888790.
            if not self.backend.cp_provider.get_basic_auth_cp().username and \
                not self.backend.cp_provider.get_basic_auth_cp().password:
                # Write the identity cert to disk
                managerlib.persist_consumer_cert(retval)
                self.backend.update()
                cp = self.backend.cp_provider.get_consumer_auth_cp()

            # FIXME: this looks like we are updating package profile as
            #        basic auth
            profile_mgr = require(PROFILE_MANAGER)
            profile_mgr.update_check(cp, retval['uuid'])

            # We have new credentials, restart virt-who
            restart_virt_who()

            self.queue.put((callback, retval, None))
        except Exception:
            self.queue.put((callback, None, sys.exc_info()))

    def _subscribe(self, uuid, current_sla, dry_run_result, callback):
        """
        Subscribe to the selected pools.
        """
        try:
            if not current_sla:
                log.debug("Saving selected service level for this system.")
                self.backend.cp_provider.get_consumer_auth_cp().updateConsumer(uuid,
                        service_level=dry_run_result.service_level)

            log.info("Binding to subscriptions at service level: %s" %
                    dry_run_result.service_level)
            for pool_quantity in dry_run_result.json:
                pool_id = pool_quantity['pool']['id']
                quantity = pool_quantity['quantity']
                log.debug("  pool %s quantity %s" % (pool_id, quantity))
                self.plugin_manager.run("pre_subscribe", consumer_uuid=uuid,
                                        pool_id=pool_id, quantity=quantity)
                ents = self.backend.cp_provider.get_consumer_auth_cp().bindByEntitlementPool(uuid, pool_id, quantity)
                self.plugin_manager.run("post_subscribe", consumer_uuid=uuid, entitlement_data=ents)
            managerlib.fetch_certificates(self.backend.certlib)
        except Exception:
            # Going to try to update certificates just in case we errored out
            # mid-way through a bunch of binds:
            # FIXME: emit update-ent-certs signal
            try:
                managerlib.fetch_certificates(self.backend.certlib)
            except Exception, cert_update_ex:
                log.info("Error updating certificates after error:")
                log.exception(cert_update_ex)
            self.queue.put((callback, None, sys.exc_info()))
            return
        self.queue.put((callback, None, None))

    # This guy is really ugly to run in a thread, can we run it
    # in the main thread with just the network stuff threaded?
    def _find_suitable_service_levels(self, consumer_uuid, facts):

        # FIXME:
        self.backend.update()

        consumer_json = self.backend.cp_provider.get_consumer_auth_cp().getConsumer(
                consumer_uuid)

        if 'serviceLevel' not in consumer_json:
            raise ServiceLevelNotSupportedException()

        owner_key = consumer_json['owner']['key']

        # This is often "", set to None in that case:
        current_sla = consumer_json['serviceLevel'] or None

        if len(self.backend.cs.installed_products) == 0:
            raise NoProductsException()

        if len(self.backend.cs.valid_products) == len(self.backend.cs.installed_products) and \
                len(self.backend.cs.partial_stacks) == 0:
            raise AllProductsCoveredException()

        if current_sla:
            available_slas = [current_sla]
            log.debug("Using system's current service level: %s" %
                    current_sla)
        else:
            available_slas = self.backend.cp_provider.get_consumer_auth_cp().getServiceLevelList(owner_key)
            log.debug("Available service levels: %s" % available_slas)

        # Will map service level (string) to the results of the dry-run
        # autobind results for each SLA that covers all installed products:
        suitable_slas = {}

        # eek, in a thread
        action_client = ActionClient(facts=facts)
        action_client.update()

        for sla in available_slas:
            dry_run_json = self.backend.cp_provider.get_consumer_auth_cp().dryRunBind(consumer_uuid, sla)
            dry_run = DryRunResult(sla, dry_run_json, self.backend.cs)

            # If we have a current SLA for this system, we do not need
            # all products to be covered by the SLA to proceed through
            # this wizard:
            if current_sla or dry_run.covers_required_products():
                suitable_slas[sla] = dry_run
        return (current_sla, self.backend.cs.unentitled_products.values(), suitable_slas)

    def _find_service_levels(self, consumer_uuid, facts, callback):
        """
        method run in the worker thread.
        """
        try:
            suitable_slas = self._find_suitable_service_levels(consumer_uuid, facts)
            self.queue.put((callback, suitable_slas, None))
        except Exception:
            self.queue.put((callback, None, sys.exc_info()))

    def _refresh(self, callback):
        try:
            managerlib.fetch_certificates(self.backend.certlib)
            self.queue.put((callback, None, None))
        except Exception:
            self.queue.put((callback, None, sys.exc_info()))

    def _watch_thread(self):
        """
        glib idle method to watch for thread completion.
        runs the provided callback method in the main thread.
        """
        try:
            (callback, retval, error) = self.queue.get(block=False)
            if error:
                callback(retval, error=error)
            else:
                callback(retval)
            return False
        except Queue.Empty:
            return True

    def get_owner_list(self, username, callback):
        ga_GObject.idle_add(self._watch_thread)
        threading.Thread(target=self._get_owner_list,
                         name="GetOwnerListThread",
                         args=(username, callback)).start()

    def get_environment_list(self, owner_key, callback):
        ga_GObject.idle_add(self._watch_thread)
        threading.Thread(target=self._get_environment_list,
                         name="GetEnvironmentListThread",
                         args=(owner_key, callback)).start()

    def register_consumer(self, name, facts, owner, env, activation_keys, callback):
        """
        Run consumer registration asyncronously
        """
        ga_GObject.idle_add(self._watch_thread)
        threading.Thread(target=self._register_consumer,
                         name="RegisterConsumerThread",
                         args=(name, facts, owner,
                               env, activation_keys, callback)).start()

    def subscribe(self, uuid, current_sla, dry_run_result, callback):
        ga_GObject.idle_add(self._watch_thread)
        threading.Thread(target=self._subscribe,
                         name="SubscribeThread",
                         args=(uuid, current_sla,
                               dry_run_result, callback)).start()

    def find_service_levels(self, consumer_uuid, facts, callback):
        ga_GObject.idle_add(self._watch_thread)
        threading.Thread(target=self._find_service_levels,
                         name="FindServiceLevelsThread",
                         args=(consumer_uuid, facts, callback)).start()

    def refresh(self, callback):
        ga_GObject.idle_add(self._watch_thread)
        threading.Thread(target=self._refresh,
                         name="RefreshThread",
                         args=(callback,)).start()


class DoneScreen(Screen):
    gui_file = "done_box"

    def __init__(self, parent):
        super(DoneScreen, self).__init__(parent)
        self.pre_message = "We are done."


class InfoScreen(Screen):
    """
    An informational screen taken from rhn-client-tools and only displayed
    in firstboot when we're not working alongside that package. (i.e.
    Fedora or RHEL 7 and beyond)

    Also allows the user to skip registration if they wish.
    """
    widget_names = Screen.widget_names + [
                'register_radio',
                'skip_radio',
                'why_register_dialog'
        ]
    gui_file = "registration_info"

    def __init__(self, parent):
        super(InfoScreen, self).__init__(parent)
        self.button_label = _("Next")
        callbacks = {
                "on_why_register_button_clicked":
                    self._on_why_register_button_clicked,
                "on_back_to_reg_button_clicked":
                    self._on_back_to_reg_button_clicked
            }

        # FIXME: self.conntect_signals to wrap self.gui.connect_signals
        self.connect_signals(callbacks)

    def pre(self):
        return False

    def apply(self):
        if self.register_radio.get_active():
            log.debug("Proceeding with registration.")
            return CHOOSE_SERVER_PAGE
        else:
            log.debug("Skipping registration.")
            return FINISH

    def post(self):
        pass

    def _on_why_register_button_clicked(self, button):
        self.why_register_dialog.show()

    def _on_back_to_reg_button_clicked(self, button):
        self.why_register_dialog.hide()
