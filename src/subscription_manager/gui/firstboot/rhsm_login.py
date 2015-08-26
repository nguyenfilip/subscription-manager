
import gettext
import socket
import sys
import logging

_ = lambda x: gettext.ldgettext("rhsm", x)


from subscription_manager import ga_loader
ga_loader.init_ga()

from subscription_manager.ga import Gtk as ga_Gtk
from subscription_manager.ga import gtk_compat

gtk_compat.threads_init()

#import gtk

#gtk.gdk.threads_init()

import rhsm

sys.path.append("/usr/share/rhsm")

# enable logging for firstboot
from subscription_manager import logutil
logutil.init_logger()

log = logging.getLogger("rhsm-app." + __name__)

# neuter linkify in firstboot
from subscription_manager.gui.utils import running_as_firstboot
running_as_firstboot()

from subscription_manager.injectioninit import init_dep_injection
init_dep_injection()

#from subscription_manager.injection import PLUGIN_MANAGER, IDENTITY, require
from subscription_manager import injection as inj

from subscription_manager.facts import Facts
from subscription_manager.hwprobe import Hardware
from subscription_manager.gui import managergui
from subscription_manager.gui import registergui
from subscription_manager.gui.utils import handle_gui_exception, format_exception
from subscription_manager.gui.autobind import \
        ServiceLevelNotSupportedException, NoProductsException, \
        AllProductsCoveredException
from subscription_manager import managerlib

from subscription_manager.i18n import configure_i18n

from firstboot import module
from firstboot import constants

configure_i18n(with_glade=True)

# Number of total RHSM firstboot screens, used to skip past to whatever's
# next in a couple places.
NUM_RHSM_SCREENS = 4

from rhsm.connection import RestlibException
from rhsm.utils import remove_scheme

sys.path.append("/usr/share/rhn")
rhn_config = None

try:
    from up2date_client import config as rhn_config
except ImportError:
    log.debug("no rhn-client-tools modules could be imported")

MANUALLY_SUBSCRIBE_PAGE = 11


class SelectSLAScreen(registergui.SelectSLAScreen):
    """
    override the default SelectSLAScreen to jump to the manual subscribe page.
    """
    def _on_get_service_levels_cb(self, result, error=None):
        if error is not None:
            if isinstance(error[1], ServiceLevelNotSupportedException):
                message = _("Unable to auto-attach, server does not support "
                            "service levels. Please run 'Subscription Manager' "
                            "to manually attach a subscription.")
                self._parent.manual_message = message
                self._parent.pre_done(MANUALLY_SUBSCRIBE_PAGE)
            elif isinstance(error[1], NoProductsException):
                message = _("No installed products on system. No need to "
                            "update subscriptions at this time.")
                self._parent.manual_message = message
                self._parent.pre_done(MANUALLY_SUBSCRIBE_PAGE)
            elif isinstance(error[1], AllProductsCoveredException):
                message = _("All installed products are fully subscribed.")
                self._parent.manual_message = message
                self._parent.pre_done(MANUALLY_SUBSCRIBE_PAGE)
            else:
                handle_gui_exception(error, _("Error subscribing"),
                                     self._parent.window)
                self._parent.finish_registration(failed=True)
            return

        (current_sla, unentitled_products, sla_data_map) = result

        self._parent.current_sla = current_sla
        if len(sla_data_map) == 1:
            # If system already had a service level, we can hit this point
            # when we cannot fix any unentitled products:
            if current_sla is not None and \
                    not self._can_add_more_subs(current_sla, sla_data_map):
                message = _("Unable to attach any additional subscriptions at "
                            "current service level: %s") % current_sla
                self._parent.manual_message = message
                self._parent.pre_done(MANUALLY_SUBSCRIBE_PAGE)
                return

            self._dry_run_result = sla_data_map.values()[0]
            self._parent.pre_done(registergui.CONFIRM_SUBS_PAGE)
        elif len(sla_data_map) > 1:
            self._sla_data_map = sla_data_map
            self.set_model(unentitled_products, sla_data_map)
            self._parent.pre_done(registergui.DONT_CHANGE)
        else:
            message = _("No service levels will cover all installed products. "
                "Please run 'Subscription Manager' to manually "
                "attach subscriptions.")
            self._parent.manual_message = message
            self._parent.pre_done(MANUALLY_SUBSCRIBE_PAGE)


class PerformRegisterScreen(registergui.PerformRegisterScreen):

    def _on_registration_finished_cb(self, new_account, error=None):
        if error is not None:
            handle_gui_exception(error, registergui.REGISTER_ERROR,
                    self._parent.window)
            self._parent.finish_registration(failed=True)
            return

        try:
            managerlib.persist_consumer_cert(new_account)
            self._parent.backend.cs.force_cert_check()  # Ensure there isn't much wait time

            if self._parent.activation_keys:
                self._parent.pre_done(registergui.REFRESH_SUBSCRIPTIONS_PAGE)
            elif self._parent.skip_auto_bind:
                message = _("You have opted to skip auto-attach.")
                self._parent.manual_message = message
                self._parent.pre_done(MANUALLY_SUBSCRIBE_PAGE)
            else:
                self._parent.pre_done(registergui.SELECT_SLA_PAGE)

        # If we get errors related to consumer name on register,
        # go back to the credentials screen where we set the
        # consumer name. See bz#865954
        except RestlibException, e:
            handle_gui_exception(e, registergui.REGISTER_ERROR,
                self._parent.window)
            if e.code == 404 and self._parent.activation_keys:
                self._parent.pre_done(registergui.ACTIVATION_KEY_PAGE)
            if e.code == 400:
                self._parent.pre_done(registergui.CREDENTIALS_PAGE)

        except Exception, e:
            handle_gui_exception(e, registergui.REGISTER_ERROR,
                    self._parent.window)
            self._parent.finish_registration(failed=True)

    def pre(self):
        # TODO: this looks like it needs updating now that we run
        # firstboot without rhn client tools.

        # Because the RHN client tools check if certs exist and bypass our
        # firstboot module if so, we know that if we reach this point and
        # identity certs exist, someone must have hit the back button.
        # TODO: i'd like this call to be inside the async progress stuff,
        # since it does take some time
        identity = inj.require(inj.IDENTITY)
        if identity.is_valid():
            try:
                managerlib.unregister(self._parent.backend.cp_provider.get_consumer_auth_cp(),
                        self._parent.identity.uuid)
            except socket.error, e:
                handle_gui_exception(e, e, self._parent.window)
            self._parent._registration_finished = False

        return registergui.PerformRegisterScreen.pre(self)


class ManuallySubscribeScreen(registergui.Screen):
    widget_names = registergui.Screen.widget_names + ['title']
    gui_file = "manually_subscribe"

    def __init__(self, parent, backend):
        super(ManuallySubscribeScreen, self).__init__(parent, backend)

        self.button_label = _("Finish")

    def apply(self):
        return registergui.FINISH

    def pre(self):
        if self._parent.manual_message:
            self.title.set_label(self._parent.manual_message)
        # XXX set message here.
        return False


class moduleClass(module.Module, object):

    def __init__(self):
        """
        Create a new firstboot Module for the 'register' screen.
        """
	super(moduleClass, self).__init__()


	self.mode = constants.MODE_REGULAR
	self.title = _("Subscription Management Registration")
	self.sidebarTitle = _("Subscription Registration")
	self.priority = 200.1


	# NOTE: all of this is copied form former firstboot_base module
        # and may no longer be needed
	# set this so subclasses can override behaviour if needed
	self._is_compat = False
	self._RESULT_SUCCESS = constants.RESULT_SUCCESS
	self._RESULT_FAILURE = constants.RESULT_FAILURE
	self._RESULT_JUMP = constants.RESULT_JUMP
	

        reg_info = registergui.RegisterInfo()
        backend = managergui.Backend()
        self.plugin_manager = inj.require(inj.PLUGIN_MANAGER)
        self.register_widget = registergui.RegisterWidget(backend, Facts(), reg_info)
	#registergui.RegisterScreen.__init__(self, backend, Facts())

	# self._add_our_screens(backend, reg_info)

        # Will be False if we are on an older RHEL version where
        # rhn-client-tools already does some things so we don't have to.
        self.standalone = True
        distribution = Hardware().get_distribution()
        log.debug("Distribution: %s" % str(distribution))

        try:
            dist_version = float(distribution[1])
            # We run this for Fedora as well, but all we really care about here
            # is if this is prior to RHEL 7, so this comparison should be safe.
            if dist_version < 7:
                self.standalone = False
        except Exception, e:
            log.error("Unable to parse a distribution version.")
            log.exception(e)
        log.debug("Running standalone firstboot: %s" % self.standalone)

        self.manual_message = None

        self._skip_apply_for_page_jump = False
        self._cached_credentials = None
        self._registration_finished = False

        self.interface = None

        self.proxies_were_enabled_from_gui = None
        self._apply_result = constants.RESULT_FAILURE

	self.page_status = constants.RESULT_FAILURE


    def apply(self, interface, testing=False):
        """
        'Next' button has been clicked - try to register with the
        provided user credentials and return the appropriate result
        value.
        """
	log.debug("interface=%s, page_status=%s", interface, self.page_status)
        self.interface = interface

	self.register_widget.emit('proceed')

	log.debug("post emit")
        #while ga_Gtk.events_pending():
        #    ga_Gtk.main_iteration()

	log.debug("post iteration")
	return self.page_status
        # bad proxy settings can cause socket.error or friends here
        # see bz #810363
#        try:
#            valid_registration = self.register()
#
#        except socket.error, e:
#            handle_gui_exception(e, e, self.window)
#            return self._RESULT_FAILURE

        # run main_iteration till we have no events, like idle
        # loop sources, aka, the thread watchers are finished.

#        if valid_registration:
#            self._cached_credentials = self._get_credentials_hash()

        # finish_registration/skip_remaining_screens should set
        # __apply_result to RESULT_JUMP
#        return self._apply_result


    def createScreen(self):
        """
        Create a new instance of gtk.VBox, pulling in child widgets from the
        glade file.
        """
        self.vbox = ga_Gtk.VBox()
	#self.vbox.pack_start(self.get_widget("register_widget"), False, False, 0)
        self.vbox.pack_start(self.register_widget.register_widget, False, False, 0)

        self.register_widget.connect('finished', self.on_finished)
        self.register_widget.connect('register-error', self.on_register_error)
	self.register_widget.register_notebook.connect('switch-page', self.on_switch_page)
	# Get rid of the 'register' and 'cancel' buttons, as we are going to
        # use the 'forward' and 'back' buttons provided by the firsboot module
        # to drive the same functionality
        #self._destroy_widget('register_button')
        #self._destroy_widget('cancel_button')

        # In firstboot, we leverage the RHN setup proxy settings already
        # presented to the user, so hide the choose server screen's proxy
        # text and button. But, if we are standalone, show our versions.
        if not self.standalone and False:
            screen = self._screens[registergui.CHOOSE_SERVER_PAGE]
            screen.proxy_frame.destroy()
    
    def on_register_error(self, obj, msg, exc_list):                             
        log.debug("register_dialog.on_register_error obj=%s msg=%s exc_list=%s", 
                  obj, msg, exc_list)                                            
                                                                                 
	self.page_status = constants.RESULT_FAILURE
        # TODO: we can add the register state, error type (error or exc)         
        if exc_list:                                                             
            self.handle_register_exception(obj, msg, exc_list)                   
        else:                                                                    
            self.handle_register_error(obj, msg)                                 
        return True                                                              

    def on_finished(self, obj):
	log.debug("on_finished obj=%s page_status=%s", obj, self.page_status)
	self.finished = True
	self.page_status = constants.RESULT_SUCCESS
	log.debug("on_finished(end) obj=%s page_status=%s", obj, self.page_status)
        return False

    def on_switch_page(self, notebook, page, page_num):
	log.debug("on_switch_page page=%s page_num=%s", page, page_num)
        return True

    def focus(self):
        """
        Focus the initial UI element on the page, in this case the
        login name field.
        """
        # FIXME:  This is currently broken
        # login_text = self.glade.get_widget("account_login")
        # login_text.grab_focus()

    def initializeUI(self):
	log.debug("initializeUi %s", self)
        # Need to make sure that each time the UI is initialized we reset back
        # to the main register screen.

        # Note, even if we are standalone firstboot mode (no rhn modules),
        # we may still have RHN installed, and possibly configured.
        self._read_rhn_proxy_settings()

        self.register_widget.initialize()
    
    def needsNetwork(self):
        """
        This lets firstboot know that networking is required, in order to
        talk to hosted UEP.
        """
        return True

    def needsReboot(self):
        return False
    
    def renderModule(self, interface):
        #ParentClass.renderModule(self, interface)

	# firstboot module class docs state to not override renderModule,
        # so this is breaking the law. 
	#
	# This is to set line wrapping on the title label to resize
	# correctly with our long titles and their even longer translations
        super(moduleClass, self).renderModule(interface)

	# FIXME: likely all of this should be behind a try/except, since it's
        #        likely to break, and it is just to fix cosmetic issues.
	# Walk down widget tree to find the title label
        label_container = self.vbox.get_children()[0]
        title_label = label_container.get_children()[0]

        # Set the title to wrap and connect to size-allocate to
        # properly resize the label so that it takes up the most
        # space it can.
        title_label.set_line_wrap(True)
        title_label.connect('size-allocate',
                             lambda label, size: label.set_size_request(size.width - 1, -1))
	 
    def shouldAppear(self):
        """
        Indicates to firstboot whether to show this screen.  In this case
        we want to skip over this screen if there is already an identity
        certificate on the machine (most likely laid down in a kickstart).
        """
        identity = inj.require(inj.IDENTITY)
        return not identity.is_valid()
    
############################################
# Everything below here is implementation  # 
############################################
                                                                                 
    def handle_register_error(self, obj, msg):                                   
        self.error_dialog(msg)                                              
                                                                                 
        # RegisterWidget.do_register_error() will take care of changing screens  
                                                                                 
    def handle_register_exception(self, obj, msg, exc_info):                     
        message = format_exception(exc_info, msg)                                
        self.error_dialog(message)                                          

    def error_dialog(self, text):
        dlg = ga_Gtk.MessageDialog(None, 0, ga_Gtk.MessageType.ERROR,
                                   ga_Gtk.ButtonsType.OK, text)
        dlg.set_markup(text)
	dlg.set_position(ga_Gtk.WindowPosition.CENTER)
        #dlg.connect('::response', dlg.destroy)
	#dlg.set_modal(True)
        rc = dlg.run()
        log.debug("dlg rc=%s", rc)

    def _add_our_screens(self, backend, reg_info):
        #insert our new screens
        self.register_widget.add_screen(6, SelectSLAScreen)
        screen = SelectSLAScreen(self, backend)
        screen.index = self._screens[registergui.SELECT_SLA_PAGE].index
        self._screens[registergui.SELECT_SLA_PAGE] = screen
        self.register_notebook.remove_page(screen.index)
        self.register_notebook.insert_page(screen.container,
                                           position=screen.index)

        screen = PerformRegisterScreen(self, backend)
        self._screens[registergui.PERFORM_REGISTER_PAGE] = screen

        screen = ManuallySubscribeScreen(self, backend)
        self._screens.append(screen)
        screen.index = self.register_notebook.append_page(screen.container)

    def _get_initial_screen(self):
        """
        Override parent method as in some cases, we use a different
        starting screen.
        """
        if self.standalone:
            return registergui.INFO_PAGE
        else:
            return registergui.CHOOSE_SERVER_PAGE

    @property
    def error_screen(self):
        return self._get_initial_screen()

    def _read_rhn_proxy_settings(self):
        if not rhn_config:
            return

        # Read and store rhn-setup's proxy settings, as they have been set
        # on the prior screen (which is owned by rhn-setup)
        up2date_cfg = rhn_config.initUp2dateConfig()
        cfg = rhsm.config.initConfig()

        # Track if we have changed this in the gui proxy dialog, if
        # we have changed it to disabled, then we apply "null", otherwise
        # if the version off the fs was disabled, we ignore the up2date proxy settings.
        #
        # Don't do anything if proxies aren't enabled in rhn config.
        if not up2date_cfg['enableProxy']:
            if self.proxies_were_enabled_from_gui:
                cfg.set('server', 'proxy_hostname', '')
                cfg.set('server', 'proxy_port', '')
                self.backend.cp_provider.set_connection_info()

            return

        # If we get here, we think we are enabling or updating proxy info
        # based on changes from the gui proxy settings dialog, so take that
        # to mean that enabledProxy=0 means to unset proxy info, not just to
        # not override it.
        self.proxies_were_enabled_from_gui = up2date_cfg['enableProxy']

        proxy = up2date_cfg['httpProxy']
        if proxy:
            # Remove any URI scheme provided
            proxy = remove_scheme(proxy)
            try:
                host, port = proxy.split(':')
                # the rhn proxy value is unicode, assume we can
                # cast to ascii ints
                port = str(int(port))
                cfg.set('server', 'proxy_hostname', host)
                cfg.set('server', 'proxy_port', port)
            except ValueError:
                cfg.set('server', 'proxy_hostname', proxy)
                cfg.set('server', 'proxy_port',
                        rhsm.config.DEFAULT_PROXY_PORT)

        if up2date_cfg['enableProxyAuth']:
            cfg.set('server', 'proxy_user', up2date_cfg['proxyUser'])
            cfg.set('server', 'proxy_password',
                    up2date_cfg['proxyPassword'])

        self.backend.cp_provider.set_connection_info()

    def close_window(self):
        """
        Overridden from RegisterScreen - we want to bypass the default behavior
        of hiding the GTK window.
        """
        pass

    def emit_consumer_signal(self):
        """
        Overriden from RegisterScreen - we don't care about consumer update
        signals.
        """
        pass

    def _destroy_widget(self, widget_name):
        """
        Destroy a widget by name.

        See gtk.Widget.destroy()
        """
        widget = self.get_object(widget_name)
        widget.destroy()

    def _set_navigation_sensitive(self, sensitive):
        # we are setting the firstboot next/back buttons
        # insensitive here, instead of the register/cancel
        # buttons this calls if shown in standalone gui.
        # But, to get to those, we need a reference to the
        # firstboot interface instance.
        # In rhel6.4, we don't get a handle on interface, until we
        # module.apply(). We call _set_navigation_sensitive from
        # module.show() (to set these back if they have changed in
        # the standalone gui flow), which is before apply(). So
        # do nothing here if we haven't set a ref to self.interface
        # yet. See bz#863572
        # EL5:
        if self._is_compat:
            self.compat_parent.backButton.set_sensitive(sensitive)
            self.compat_parent.nextButton.set_sensitive(sensitive)
        # EL6:
        else:
            if self.interface is not None:
                self.interface.backButton.set_sensitive(sensitive)
                self.interface.nextButton.set_sensitive(sensitive)

    def _get_credentials_hash(self):
        """
        Return an internal hash representation of the text input
        widgets.  This is used to compare if we have changed anything
        when moving back and forth across modules.
        """
        return {"username": self.username,
                "password": self.password,
                "consumername": self.consumername,
        }

    def _get_text(self, widget_name):
        """
        Return the text value of an input widget referenced
        by name.
        """
        widget = self.get_object(widget_name)
        return widget.get_text()

    def _set_register_label(self, screen):
        """
        Overridden from registergui to disable changing the firstboot button
        labels.
        """
        pass

    def finish_registration(self, failed=False):
        log.info("Finishing registration, failed=%s" % failed)
        if failed:
            self._set_navigation_sensitive(True)
            self._set_initial_screen()
        else:
            self._registration_finished = True
            self._skip_remaining_screens(self.interface)
            registergui.RegisterScreen.finish_registration(self, failed=failed)

    def _skip_remaining_screens(self, interface):
        """
        Find the first non-rhsm module after the rhsm modules, and move to it.

        Assumes that there is only _one_ rhsm screen
        """
        if self._is_compat:
            # el5 is easy, we can just pretend the next button was clicked,
            # and tell our own logic not to run for the button press.
            self._skip_apply_for_page_jump = True
            self.compat_parent.nextClicked()
        else:
            self._apply_result = self._RESULT_SUCCESS
            return

