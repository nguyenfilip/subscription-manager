#
# Copyright (c) 2012 Red Hat, Inc.
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

import sys

sys.path.append("/usr/share/rhsm")

# rhsm_login init the injector before we are loaded
from subscription_manager import injection as inj
from subscription_manager.i18n import configure_i18n

from firstboot import module
from firstboot import constants

configure_i18n(with_glade=True)

# Number of total RHSM firstboot screens, used to skip past to whatever's
# next in a couple places.
NUM_RHSM_SCREENS = 4

#from firstboot.constants import RESULT_SUCCESS, RESULT_FAILURE, RESULT_JUMP
#from firstboot.module import Module
ParentClass = Module


class RhsmFirstbootModule(module.Module):

    def __init__(self, title, sidebar_title, priority, compat_priority):
        ParentClass.__init__(self)

        if _version == "el6":
            # set this so subclasses can override behaviour if needed
            self._is_compat = False
            self._RESULT_SUCCESS = RESULT_SUCCESS
            self._RESULT_FAILURE = RESULT_FAILURE
            self._RESULT_JUMP = RESULT_JUMP
        else:
            self._is_compat = True
            self._RESULT_SUCCESS = True
            self._RESULT_FAILURE = None
            self._RESULT_JUMP = True

        # this value is relative to when you want to load the screen
        # so check other modules before setting
        self.priority = priority
        self.sidebarTitle = sidebar_title
        self.title = title

        # el5 values
        self.runPriority = compat_priority
        self.moduleName = self.sidebarTitle
        self.windowTitle = self.moduleName
        self.shortMessage = self.title
        self.noSidebar = True

        # el5 value to get access to parent object for page jumping
        self.needsparent = 1

    def renderModule(self, interface):
        ParentClass.renderModule(self, interface)
        label_container = self.vbox.get_children()[0]
        title_label = label_container.get_children()[0]

        # Set the title to wrap and connect to size-allocate to
        # properly resize the label so that it takes up the most
        # space it can.
        title_label.set_line_wrap(True)
        title_label.connect('size-allocate', lambda label, size: label.set_size_request(size.width - 1, -1))

    def needsNetwork(self):
        """
        This lets firstboot know that networking is required, in order to
        talk to hosted UEP.
        """
        return True

    def shouldAppear(self):
        """
        Indicates to firstboot whether to show this screen.  In this case
        we want to skip over this screen if there is already an identity
        certificate on the machine (most likely laid down in a kickstart).
        """
        identity = inj.require(inj.IDENTITY)
        return not identity.is_valid()

