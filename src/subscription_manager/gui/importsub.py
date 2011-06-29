#
# Copyright (c) 2010 Red Hat, Inc.
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
import os
import shutil
import logging

_ = gettext.gettext

import rhsm.config

from subscription_manager.gui import widgets
from subscription_manager.gui.utils import errorWindow

from rhsm.certificate import EntitlementCertificate

cfg = rhsm.config.initConfig()
ENT_CONFIG_DIR = cfg.get('rhsm', 'entitlementCertDir')

log = logging.getLogger('rhsm-app.' + __name__)


class ImportSubDialog(widgets.GladeWidget):
    """
    Dialog to manually import an entitlement certificate for this machine.
    Generally used for disconnected (unregistered) systems.
    """
    def __init__(self):

        widget_names = [
                'main_dialog',
                'certificate_chooser_button',
        ]
        super(ImportSubDialog, self).__init__('importsub.glade', widget_names)

        dic = {
                "on_import_cancel_button_clicked": self._cancel_button_clicked,
                "on_certificate_import_button_clicked": self._import_button_clicked,
            }
        self.glade.signal_autoconnect(dic)

        self.main_dialog.connect("hide", self._cancel_button_clicked)
        self.main_dialog.connect("delete_event", self._delete_event)

    def _cancel_button_clicked(self, button=None):
        self.main_dialog.hide()
        return True

    def show(self):
        self.main_dialog.present()

    def _delete_event(self, event, data=None):
        return self._cancel_button_clicked()

    def _import_button_clicked(self, button):
        src_cert_file = self.certificate_chooser_button.get_filename()
        if src_cert_file is None:
            errorWindow(_("You must select a certificate."))
            return False

        try:
            # Check for a valid entitlement certificate:
            ent_cert = EntitlementCertificate()
            ent_cert.read(src_cert_file)
            if ent_cert.bogus():
                raise Exception("Invalid X509 entitlement certificate.")
        except Exception, e:
            log.error("Error parsing manually imported entitlement "
                    "certificate: %s" % src_cert_file)
            log.exception(e)
            errorWindow(_("%s is not a valid certificate file. Please upload a valid certificate.") %
                os.path.basename(src_cert_file))
            return False

        if not os.access(ENT_CONFIG_DIR, os.R_OK):
            os.mkdir(ENT_CONFIG_DIR)

        dest_file_path = os.path.join(ENT_CONFIG_DIR, os.path.basename(src_cert_file))
        shutil.copy(src_cert_file, dest_file_path)
        self.certificate_chooser_button.unselect_all()
        self.main_dialog.hide()

    def set_parent_window(self, window):
        self.main_dialog.set_transient_for(window)