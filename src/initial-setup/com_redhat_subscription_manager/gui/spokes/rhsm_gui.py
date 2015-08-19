#
# Copyright (C) 2015  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

import logging
import sys

from pyanaconda.ui.gui.spokes import NormalSpoke
from pyanaconda.ui.common import FirstbootOnlySpokeMixIn
from pyanaconda.ui.categories.system import SystemCategory
from pyanaconda.ui.gui.utils import really_hide

log = logging.getLogger(__name__)

RHSM_PATH = "/usr/share/rhsm"
sys.path.append(RHSM_PATH)

from subscription_manager import ga_loader

# initial-setup only works with gtk version 3
ga_loader.init_ga(gtk_version="3")

from subscription_manager.ga import GObject as ga_GObject
from subscription_manager.gui import managergui
from subscription_manager.injectioninit import init_dep_injection
from subscription_manager import injection as inj
from subscription_manager.gui import registergui

ga_GObject.threads_init()

__all__ = ["RHSMSpoke"]


class RHSMSpoke(FirstbootOnlySpokeMixIn, NormalSpoke):
    buildrObjects = ["RHSMSpokeWindow"]

    mainWidgetName = "RHSMSpokeWindow"

    uiFile = "rhsm_gui.ui"

    category = SystemCategory

    icon = "face-cool-symbolic"

    title = "Subscription Manager"

    def __init__(self, data, storage, payload, instclass):
        NormalSpoke.__init__(self, data, storage, payload, instclass)
        self._done = False

    def initialize(self):
        NormalSpoke.initialize(self)
        self._done = False
        init_dep_injection()

        facts = inj.require(inj.FACTS)
        backend = managergui.Backend()

        self.register_widget = registergui.RegisterWidget(backend, facts,
                                                          parent=self.main_window)
        self.register_box = self.builder.get_object("register_box")
        self.button_box = self.builder.get_object('navigation_button_box')
        self.proceed_button = self.builder.get_object('proceed_button')
        self.cancel_button = self.builder.get_object('cancel_button')

        self.register_box.pack_start(self.register_widget.register_widget,
                                     True, True, 0)

        # Hook up the nav buttons in the gui
        # TODO: add a 'start over'?
        self.proceed_button.connect('clicked', self._on_register_button_clicked)
        self.cancel_button.connect('clicked', self.cancel)

        # initial-setup will likely
        #self.register_widget.connect('finished', self.finished)
        #self.register_widget.connect('register-error', self.on_register_error)
        #self.register_widget.connect('register-failure', self._on_register_failure)
        #self.register_widget.connect('attach-error', self.on_attach_error)
        #self.register_widget.connect('attach-failure', self._on_attach_failure)
        #self.register_widget.connect('register-error-foo', self._on_register_error_foo)
        self.register_widget.connect('register-error', self._on_error)

        # update the 'next/register button on page change'
        self.register_widget.connect('notify::register-button-label',
                                       self._on_register_button_label_change)

        # update completed and status on register state changes
        self.register_widget.connect('notify::register-state',
                                     self._on_register_state_change)

        self.register_box.show_all()
        self.register_widget.initialize()

    def _on_error(self, widget, *args):
        log.debug("-on_error widget=%s args=%s", widget, args)
        print "RHSMSPoke._on_error"
        self.set_error("%s" % args)

    # callback to attach to RegisterWidgets finished signal
    def finished(self, button):
        self._done = True
        self.register_widget.done()
        really_hide(self.button_box)

    # Update gui widgets to reflect state of self.data
    # This could also be used to pre populate partial answers from a ks
    # or answer file
    def refresh(self):
        log.debug("data.addons.com_redhat_subscription_manager %s",
                  self.data.addons.com_redhat_subscription_manager)

        pass

    # take info from the gui widgets and set into the self.data
    def apply(self):
        self.data.addons.com_redhat_subscription_manager.text = \
            "System is registered to Red Hat Subscription Management."

    # when the spoke is left, then can run anything that happens
    def execute(self):
        pass

    def cancel(self, button):
        # TODO: clear out settings and restart?
        # TODO: attempt to undo the REST api calls we've made?
        pass

    # A property indicating the spoke is ready to be visited. This
    # could depend on other modules or waiting for internal state to be setup.
    @property
    def ready(self):
        return True

    # Indicate if all the mandotory actions are completed
    @property
    def completed(self):
        # TODO: tie into register_widget.info.register-state
        return self._done

    # indicate if the module has to be completed before initial-setup is done.
    @property
    def mandatory(self):
        return False

    # A user facing string showing a summary of the status. This is displayed
    # under the spokes name on it's hub.
    @property
    def status(self):
        if self._done:
            return "System is registered to RHSM."
        else:
            return "System is not registered to RHSM."

    def _on_register_button_clicked(self, button):
        log.debug("dialog on_register_button_clicked, button=%s, %s", button, self.register_widget)
        self.clear_info()
        self.register_widget.emit('proceed')

    # FIXME: REMOVE: replace on_register_error
    def _on_register_error_foo(self, widget, msg):
        log.debug("rhsm_gui _on_register_error_foo widget=%s msg=%s",
                  widget, msg)
        self.set_error(msg)

    def on_register_error(self, args):
        log.debug("register_dialog.on_register_error args=%s", args)
        # FIXME: can we just ignore this for sm-gui?
        self.register_widget.register_error_screen()

    def on_attach_error(self, args):
        log.debug("register_dialog.on_attach_error args=%s", args)
        # FIXME: can we just ignore this for sm-gui?
        self.register_widget.attach_error_screen()

    def _on_register_failure(self, args):
        # TODO: go to a failure screen, once we add it to RegisterWidget
        log.debug("_on_register_failure, args=%s", args)
        # FIXME:
        self.set_error("Register failed.")

    # If we get an 'attach-failure' signal, but suceeded in registering,
    # we can count that as 'completed'
    def _on_attach_failure(self, args):
        log.debug("_on_attach_failure, args=%s", args)
        self.set_error("Attach failed.")

    def _on_register_button_label_change(self, obj, value):
        log.debug('_on_register_button_label_change obj=%s value=%s', obj, value)
        register_label = obj.get_property('register-button-label')
        log.debug('register_label=%s', register_label)
        log.debug('self.proceed_button %s', self.proceed_button)

        if register_label:
            self.proceed_button.set_label(register_label)

    def _on_register_state_change(self, obj, value):
        state = obj.get_property('register-state')
        # If we are past registration, that's 'completed' enough
        if state != registergui.REGISTERING:
            self.completed = True
