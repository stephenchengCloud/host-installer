# Copyright (c) 2005-2006 XenSource, Inc. All use and distribution of this 
# copyrighted material is governed by and subject to terms and conditions 
# as licensed by XenSource, Inc. All other rights reserved.
# Xen, XenSource and XenEnterprise are either registered trademarks or 
# trademarks of XenSource Inc. in the United States and/or other countries.

###
# XEN CLEAN INSTALLER
# Upgrade paths
#
# written by Andrew Peace

# This stuff exists to hide ugliness and hacks that are required for upgrades
# from the rest of the installer.

import os
import re
import subprocess
import tempfile

import product
import diskutil
from disktools import *
import util
import constants
import xelogging
import backend

class UpgraderNotAvailable(Exception):
    pass

def upgradeAvailable(src):
    return __upgraders__.hasUpgrader(src.name, src.version, src.variant)

def getUpgrader(src):
    """ Returns an upgrader instance suitable for src. Propogates a KeyError
    exception if no suitable upgrader is available (caller should have checked
    first by calling upgradeAvailable). """
    return __upgraders__.getUpgrader(src.name, src.version, src.variant)(src)

class Upgrader(object):
    """ Base class for upgraders.  Superclasses should define an
    upgrades_product variable that is the product they upgrade, an 
    upgrades_variants list of Retail or OEM install types that they upgrade, and an 
    upgrades_versions that is a list of pairs of version extents they support
    upgrading."""

    requires_backup = False
    optional_backup = True
    prompt_for_target = False

    def __init__(self, source):
        """ source is the ExistingInstallation object we're to upgrade. """
        self.source = source
        self.restore_list = []

    def upgrades(cls, product, version, variant):
        return (cls.upgrades_product == product and
                variant in cls.upgrades_variants and
                True in [ _min <= version <= _max for (_min, _max) in cls.upgrades_versions ])

    upgrades = classmethod(upgrades)

    prepTargetStateChanges = []
    prepTargetArgs = []
    def prepareTarget(self, progress_callback):
        """ Modify partition layout prior to installation. """
        return

    doBackupStateChanges = []
    doBackupArgs = []
    def doBackup(self, progress_callback):
        """ Collect configuration etc from installation. """
        return

    prepStateChanges = []
    prepUpgradeArgs = []
    def prepareUpgrade(self, progress_callback):
        """ Collect any state needed from the installation, and return a
        tranformation on the answers dict. """
        return

    def buildRestoreList(self):
        """ Add filenames to self.restore_list which will be copied by
        completeUpgrade(). """
        return

    completeUpgradeArgs = ['mounts', 'primary-disk', 'backup-partnum']
    def completeUpgrade(self, mounts, target_disk, backup_partnum):
        """ Write any data back into the new filesystem as needed to follow
        through the upgrade. """

        self.tds = None

        def restore_file(f):
            src = os.path.join(self.tds, f)
            dst = os.path.join(mounts['root'], f)
            if os.path.exists(src):
                xelogging.log("Restoring /%s" % f)
                util.assertDir(os.path.dirname(dst))
                util.runCmd2(['cp', '-p', src, dst])
            else:
                xelogging.log("WARNING: /%s did not exist in the backup image." % f)

        backup_volume = PartitionTool.partitionDevice(target_disk, backup_partnum)
        try:
            self.tds = tempfile.mkdtemp(dir = "/tmp", prefix = "upgrade-src-")
            util.mount(backup_volume, self.tds, options = ['ro'])

            self.buildRestoreList()

            xelogging.log("Restoring preserved files")
            for f in self.restore_list:
                if isinstance(f, str):
                    restore_file(f)
                elif isinstance(f, dict):
                    assert 'dir' in f
                    pat = 're' in f and f['re'] or None
                    src_dir = os.path.join(self.tds, f['dir'])
                    if os.path.exists(src_dir):
                        for ff in os.listdir(src_dir):
                            fn = os.path.join(src_dir, ff)
                            if not pat or pat.match(fn):
                                restore_file(fn[len(self.tds)+1:])
        finally:
            if self.tds:
                if os.path.ismount(self.tds):
                    util.umount(self.tds)
                os.rmdir(self.tds)


class ThirdGenUpgrader(Upgrader):
    """ Upgrader class for series 5 Retail products. """
    upgrades_product = "xenenterprise"
    upgrades_versions = [ (product.Version(5, 5, 0), product.THIS_PRODUCT_VERSION) ]
    upgrades_variants = [ 'Retail' ]
    requires_backup = True
    optional_backup = False
    
    def __init__(self, source):
        Upgrader.__init__(self, source)

    doBackupArgs = ['primary-disk', 'backup-partnum']
    doBackupStateChanges = []
    def doBackup(self, progress_callback, target_disk, backup_partnum):

        progress_callback(10)

        # format the backup partition:
        backup_partition = PartitionTool.partitionDevice(target_disk, backup_partnum)
        if util.runCmd2(['mkfs.ext3', backup_partition]) != 0:
            raise RuntimeError, "Backup: Failed to format filesystem on %s" % backup_partition

        progress_callback(20)

        # copy the files across:
        primary_mount = '/tmp/backup/primary'
        backup_mount  = '/tmp/backup/backup'
        for mnt in [primary_mount, backup_mount]:
            util.assertDir(mnt)
        try:
            util.mount(self.source.root_device, primary_mount, options = ['ro'])
            util.mount(backup_partition, backup_mount)
            cmd = ['cp', '-a'] + \
                  [ os.path.join(primary_mount, x) for x in os.listdir(primary_mount) ] + \
                  ['%s/' % backup_mount]
            assert util.runCmd2(cmd) == 0
        
        finally:
            for mnt in [primary_mount, backup_mount]:
                util.umount(mnt)

    prepUpgradeArgs = ['installation-uuid', 'control-domain-uuid']
    prepStateChanges = ['installation-uuid', 'control-domain-uuid']
    def prepareUpgrade(self, progress_callback, installID, controlID):
        """ Try to preserve the installation and control-domain UUIDs from
        xensource-inventory."""
        try:
            installID = self.source.getInventoryValue("INSTALLATION_UUID")
            controlID = self.source.getInventoryValue("CONTROL_DOMAIN_UUID")
        except KeyError:
            raise RuntimeError, "Required information (INSTALLATION_UUID, CONTROL_DOMAIN_UUID) was missing from your xensource-inventory file.  Aborting installation; please replace these keys and try again."

        return installID, controlID

    def buildRestoreList(self):
        self.restore_list += ['etc/xensource/ptoken', 'etc/xensource/pool.conf', 
                              'etc/xensource/xapi-ssl.pem',
                              'etc/ssh/ssh_host_dsa_key', 'etc/ssh/ssh_host_dsa_key.pub',
                              'etc/ssh/ssh_host_key', 'etc/ssh/ssh_host_key.pub',
                              'etc/ssh/ssh_host_rsa_key', 'etc/ssh/ssh_host_rsa_key.pub']

        self.restore_list += [ 'etc/sysconfig/network' ]
        self.restore_list.append({'dir': 'etc/sysconfig/network-scripts', 're': re.compile(r'.*/ifcfg-[a-z0-9.]+')})

        self.restore_list += ['var/xapi/state.db', 'etc/xensource/license']
        self.restore_list.append({'dir': constants.FIRSTBOOT_DATA_DIR, 're': re.compile(r'.*.conf')})

        self.restore_list.append({'dir': os.path.join(constants.FIRSTBOOT_DATA_DIR, 'initial-ifcfg'),
                                  're': re.compile(r'.*/ifcfg-[a-z0-9.]+')})


    completeUpgradeArgs = ['mounts', 'installation-to-overwrite', 'primary-disk', 'backup-partnum']
    def completeUpgrade(self, mounts, prev_install, target_disk, backup_partnum):

        util.assertDir(os.path.join(mounts['root'], "var/xapi"))
        util.assertDir(os.path.join(mounts['root'], "etc/xensource"))

        Upgrader.completeUpgrade(self, mounts, target_disk, backup_partnum)

        v = product.Version(prev_install.version.major,
                            prev_install.version.minor,
                            prev_install.version.release)
        f = open(os.path.join(mounts['root'], 'var/tmp/.previousVersion'), 'w')
        f.write("PRODUCT_VERSION='%s'\n" % v)
        f.close()

        state = open(os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR, 'host.conf'), 'w')
        print >>state, "UPGRADE=true"
        state.close()

        # CA-21443: initial ifcfg files are needed for pool eject
        save_dir = os.path.join(constants.FIRSTBOOT_DATA_DIR, 'initial-ifcfg')
        if not os.path.exists(os.path.join(mounts['root'], save_dir)):
            xelogging.log("Generating firstboot ifcfg files from firstboot data")
            try:
                net_dict = util.readKeyValueFile(os.path.join(self.tds, constants.FIRSTBOOT_DATA_DIR, 'network.conf'))
                if net_dict.has_key('ADMIN_INTERFACE'):
                    mgmt_dict = util.readKeyValueFile(os.path.join(self.tds, constants.FIRSTBOOT_DATA_DIR, 'interface-%s.conf' % net_dict['ADMIN_INTERFACE']))
                    xelogging.log(mgmt_dict)
                    brname = 'xenbr%s' % mgmt_dict['LABEL'][3:]
                    eth_ifcfg = open(os.path.join(mounts['root'], save_dir, 'ifcfg-%s' % mgmt_dict['LABEL']), 'w')
                    print >>eth_ifcfg, "XEMANAGED=yes"
                    print >>eth_ifcfg, "DEVICE=%s" % mgmt_dict['LABEL']
                    print >>eth_ifcfg, "ONBOOT=no"
                    print >>eth_ifcfg, "TYPE=Ethernet"
                    print >>eth_ifcfg, "HWADDR=%s" % net_dict['ADMIN_INTERFACE']
                    print >>eth_ifcfg, "BRIDGE=%s" % brname
                    eth_ifcfg.close()

                    br_ifcfg = open(os.path.join(mounts['root'], save_dir, 'ifcfg-%s' % brname), 'w')
                    print >>br_ifcfg, "XEMANAGED=yes"
                    print >>br_ifcfg, "DEVICE=%s" % brname
                    print >>br_ifcfg, "ONBOOT=no"
                    print >>br_ifcfg, "TYPE=Bridge"
                    print >>br_ifcfg, "DELAY=0"
                    print >>br_ifcfg, "STP=0"
                    print >>br_ifcfg, "PIFDEV=%s" % mgmt_dict['LABEL']
                    if mgmt_dict['MODE'] == 'static':
                        print >>br_ifcfg, "BOOTPROTO=none"
                        print >>br_ifcfg, "NETMASK=%s" % mgmt_dict['NETMASK']
                        print >>br_ifcfg, "IPADDR=%s" % mgmt_dict['IP']
                        print >>br_ifcfg, "GATEWAY=%s" % mgmt_dict['GATEWAY']
                        i = 1
                        while mgmt_dict.has_key('DNS%d' % i):
                            print >>br_ifcfg, "DNS%d=%s" % (i, mgmt_dict['DNS%d' % i])
                            i += 1
                        if i > 1:
                            print >>br_ifcfg, "PEERDNS=yes"
                    else:
                        print >>br_ifcfg, "BOOTPROTO=dhcp"
                        print >>br_ifcfg, "PERSISTENT_DHCLIENT=yes"
                        print >>br_ifcfg, "PEERDNS=yes"
                    br_ifcfg.close()
            except:
                pass


class ThirdGenOEMUpgrader(ThirdGenUpgrader):
    """ Upgrader class for series 5 OEM products. """
    requires_backup = True
    optional_backup = False
    prompt_for_target = True
    upgrades_variants = [ 'OEM' ]

    def __init__(self, source):
        ThirdGenUpgrader.__init__(self, source)

    prepTargetArgs = ['installation-to-overwrite', 'primary-disk', 'primary-partnum', 'backup-partnum', 'storage-partnum']
    prepTargetStateChanges = ['installation-to-overwrite', 'post-backup-delete', 'root-start']
    def prepareTarget(self, progress_callback, existing, primaryDisk, primaryPartnum, backupPartnum, storagePartnum):
        """Modify partition layout prior to installation.  This method must make space on the target disk,
        resizing the SR already present if necessary, and create root and backup partitions."""
        lvmTool = LVMTool()
        partTool = PartitionTool(primaryDisk)
        storageDevice = partTool._partitionDevice(storagePartnum)
        rootByteSize = constants.root_size * 2 ** 20
        
        # Build a list of partition numbers to preserve at least for now.
        partNumsToPreserve = []
        
        # First add the utility partition if present
        if primaryPartnum == 1 :
            # No utility partition - start at 1
            foundUtilityNumber = None
        else:
            # First partition is a utility partition so preserve it
            partNumsToPreserve.append(1)
            foundUtilityNumber = 1

        firstXSPartition = primaryPartnum

        # Config partitions are found on XenServer-claimed disks in both OEM Flash and HDD installations
        # They must be preserved for now in case we are copying state information from them, but will be
        # deleted later in the upgrade process
        foundConfigDevice = lvmTool.configPartition(primaryDisk)
        if foundConfigDevice is not None:
            foundConfigNumber = partTool.partitionNumber(foundConfigDevice)
            partNumsToPreserve.append(foundConfigNumber)
        else:
            foundConfigNumber = None
        
        foundSwapDevice = lvmTool.swapPartition(primaryDisk)
        if foundSwapDevice is not None:
            # We won't need this, so delete the LVM records and don't add this to the preserved list
            lvmTool.deleteDevice(foundSwapDevice)
            foundSwapNumber = partTool.partitionNumber(foundSwapDevice)
        else:
            foundSwapNumber = None
        
        foundSRDevice = lvmTool.srPartition(primaryDisk)
        if foundSRDevice is not None:
            foundSRNumber = partTool.partitionNumber(foundSRDevice)
            partNumsToPreserve.append(foundSRNumber)
        else:
            foundSRNumber = None
        
        # Detect the OEM HDD state partition as a special case
        isOEMHDD = ( foundSRNumber == constants.OEMHDD_SR_PARTITION_NUMBER )
        if isOEMHDD:
            foundConfigNumber = foundSRNumber - 1
            partNumsToPreserve.append(foundConfigNumber)
            
        xelogging.log(
            "prepareTarget found the following:"+
            "\nUtility partition : "+str(foundUtilityNumber) +
            "\nConfig partition  : "+str(foundConfigNumber) +
            "\nSwap partition    : "+str(foundSwapNumber) +
            "\nSR partition      : "+str(foundSRNumber) +
            "\nand chose to preserve partitions: "+", ".join( [ str(i) for i in partNumsToPreserve ] )
        )
        # Purge redundant partitions on target
        partTool.deletePartitions( [ num for num, part in partTool.iteritems() if num not in partNumsToPreserve ] )
        
        if isOEMHDD:
            partTool.renamePartition(foundConfigNumber, firstXSPartition)
            if existing is not None: # existing can be None in unit testing only
                existing.partitionWasRenamed(partTool._partitionDevice(foundConfigNumber),
                    partTool._partitionDevice(firstXSPartition))
            foundConfigNumber = firstXSPartition

        if foundSRDevice is not None and not isOEMHDD:
            # There is an SR partition on this disk that we need to preserve, as as this is not OEM HDD
            # we need to resize it
            lvmSize = lvmTool.deviceSize(foundSRDevice)
            lvmTool.resizeDevice(foundSRDevice, lvmSize - 2 * rootByteSize)
            partitionSize = partTool.partitionSize(foundSRNumber)
            partTool.resizePartition(foundSRNumber, partitionSize - 2 * rootByteSize)
        
        if foundSRDevice is not None:
            # We must rename the SR partition now and not later, as in OEM HDD the SR lives in an
            # extended partition that we're deleting
            partTool.renamePartition(foundSRNumber, storagePartnum)
            if existing is not None: # existing can be None in unit testing only
                existing.partitionWasRenamed(partTool._partitionDevice(foundSRNumber),
                    partTool._partitionDevice(storagePartnum))
            # Update foundSRNumber to keep track of the SR
            foundSRNumber = storagePartnum

        # Partition creation
        # So far we've deleted everything except utility, config and SR partitions on this disk.
        # In all scenarios backupPartnum is now free for our use
        partTool.deletePartitionIfPresent(backupPartnum)
    
        # If an SR is present, root and backup partitions go at the end of the disk for Flash,
        # and the start for OEM HDD.  In either case they have lower numbers than the SR partition
        if foundUtilityNumber is not None:
            availStart = partTool.partitionEnd(foundUtilityNumber)
        else:
            availStart = partTool.sectorSize # First sector stores partition table, so skip it

        if foundSRNumber is not None:
            # SR is present, so root and backup partitions go either before (if there's room) or
            # after it
            if isOEMHDD and foundConfigNumber is not None:
                # Fit the partition between where the new root partition will end and the start
                # of the current config partition
                rootStart = availStart
                backupStart = rootStart + rootByteSize
                backupSize = partTool.partitionStart(foundConfigNumber) - backupStart
            elif partTool.partitionStart(foundSRNumber) - availStart > 2 * rootByteSize:
                # Root and backup partitions will fit before the SR...
                rootStart = availStart
                backupSize = rootByteSize
            else:
                # Won't fit - put at the end.  We know there's space because of the resize above
                rootStart = partTool.partitionEnd(foundSRNumber)
                backupSize = rootByteSize
        else:
            # No SR present - just leave space for the root partition
            rootStart = availStart
            backupSize = rootByteSize

        backupStart = rootStart + rootByteSize
        partTool.createPartition(number = backupPartnum, id = partTool.ID_LINUX,
            startBytes = backupStart, sizeBytes = backupSize)
        
        lvmTool.commit(progress_callback) # progress_callback gets values 0..100
        partTool.commit()

        # Store the number of the state partition so that we can delete it later
        postBackupDelete = []
        if foundConfigNumber is not None:
            postBackupDelete.append(foundConfigNumber)

        return existing, postBackupDelete, rootStart

    doBackupArgs = ['installation-to-overwrite', 'primary-disk', 'backup-partnum']
    doBackupStateChanges = []
    def doBackup(self, progress_callback, existing, primaryDisk, backupPartnum):
        backup_partition = PartitionTool.partitionDevice(primaryDisk, backupPartnum)
        def readDbGen(root_dir):
            gen = -1
            try:
                genfd = open(os.path.join(root_dir, 'var/xapi/state.db.generation'), 'r')
                gen = int(genfd.readline())
                genfd.close()
            except:
                pass
            return gen

        # format the backup partition:
        if util.runCmd2(['mkfs.ext3', backup_partition]) != 0:
            raise Exception,  "Backup: Failed to format filesystem on %s" % backup_partition

        primary_mount = '/tmp/backup/primary'
        backup_mount  = '/tmp/backup/backup'
        for mnt in [primary_mount, backup_mount]:
            util.assertDir(mnt)

        util.mount(backup_partition, backup_mount)

        db_generation = (-1, None, None)
        lvmTool = LVMTool()
        try:
            # copy from primary state partition:
            util.mount(existing.state_device, primary_mount, options = ['ro'])
            root_dir = os.path.join(primary_mount, existing.state_prefix)
            gen = readDbGen(root_dir)

            xelogging.log("Copying state from %s" % root_dir)
            cmd = ['cp', '-a'] + \
                  [ os.path.join(root_dir, x) for x in os.listdir(root_dir) ] + \
                  ['%s/' % backup_mount]
            assert util.runCmd2(cmd) == 0
            if gen > db_generation[0]:
                db_generation = (gen, existing.state_device, existing.state_prefix)
            
            util.umount(primary_mount)
            freqPath = os.path.join(root_dir, 'etc/freq-etc/etc')
            if os.path.isdir(freqPath): # Present on OEM Flash only
                cmd = ['cp', '-a', freqPath, '%s/' % backup_mount]
                assert util.runCmd2(cmd) == 0

            # copy from auxiliary state partitions:
            for state_info in existing.auxiliary_state_devices:
                lvmTool.activateVG(state_info['vg'])
                mountPath = os.path.join('/dev', state_info['vg'], state_info['lv'])
                util.mount(mountPath, primary_mount, options = ['ro'])
                root_dir = os.path.join(primary_mount, existing.inventory['XAPI_DB_COMPAT_VERSION'])
                gen = readDbGen(root_dir)
                
                xelogging.log("Copying state from %s" % root_dir)
                cmd = ['cp', '-a'] + \
                      [ os.path.join(root_dir, x) for x in os.listdir(root_dir) ] + \
                      ['%s/' % backup_mount]
                assert util.runCmd2(cmd) == 0
                if gen > db_generation[0]:
                    db_generation = (gen, state_info, existing.inventory['XAPI_DB_COMPAT_VERSION'])
                util.umount(primary_mount)

            # always keep the state with the highest db generation count
            if db_generation[0] > gen:
                state_info = db_generation[1]
                mountPath = os.path.join('/dev', state_info['vg'], state_info['lv'])
                util.mount(mountPath, primary_mount, options = ['ro'])
                root_dir = os.path.join(primary_mount, db_generation[2])
                
                xelogging.log("Copying state from %s" % root_dir)
                cmd = ['cp', '-a'] + \
                    [ os.path.join(root_dir, x) for x in os.listdir(root_dir) ] + \
                    ['%s/' % backup_mount]
                assert util.runCmd2(cmd) == 0
        finally:
            for mnt in [primary_mount, backup_mount]:
                util.umount(mnt)
            lvmTool.deactivateAll()

    prepUpgradeArgs = ['installation-uuid', 'control-domain-uuid', 'primary-disk', 'primary-partnum', 
        'backup-partnum', 'root-start', 'post-backup-delete']
    # Leave prepStateChanges as per superclass
    def prepareUpgrade(self, progress_callback, installID, controlID, primaryDisk, primaryPartnum, backupPartnum,
        rootStart, postBackupDelete):
        # Call the superclass method and store its return value.  This does the backup
        retVal = ThirdGenUpgrader.prepareUpgrade(self, progress_callback, installID, controlID)
        
        rootByteSize = constants.root_size * 2 ** 20
        # Delete partitions after backup
        partTool = PartitionTool(primaryDisk)
        xelogging.log('Backup complete - deleting partitions: '+','.join([str(x) for x in postBackupDelete]))
        partTool.deletePartitions(postBackupDelete)
        
        xelogging.log('Creating new primary partition as primary partition '+str(primaryPartnum))
        partTool.createPartition(number = primaryPartnum, id = partTool.ID_LINUX,
            startBytes = rootStart, sizeBytes = rootByteSize)
        
        # Set primary partition as bootable
        partTool.inactivateDisk()
        partTool.setActiveFlag(True, primaryPartnum)
        
        # Grow the backup partition to its full size, now that the state partition has gone
        partTool.resizePartition(backupPartnum, rootByteSize)
        partTool.commit()
        
        return retVal

class ThirdGenOEMDiskUpgrader(ThirdGenUpgrader):
    """ Upgrader class for series 5 OEM Disk products. """
    requires_backup = False
    optional_backup = False
    repartition     = True
    upgrades_variants = [ 'OEM' ]

    def __init__(self, source):
        ThirdGenUpgrader.__init__(self, source)

    prepUpgradeArgs = ['installation-uuid', 'control-domain-uuid', 'installation-to-overwrite']
    prepStateChanges = ['installation-uuid', 'control-domain-uuid', 'primary-disk']
    def prepareUpgrade(self, progress_callback, installID, controlID, existing):
        """ Prepare the disk for a Retail XenServer installation. """

        inst_ctrl_pd = ThirdGenUpgrader.prepareUpgrade(self, progress_callback, installID, controlID)
        disk = existing.primary_disk

        progress_callback(10)
        backend.removeExcessOemPartitions(existing)
        progress_callback(20)
        backend.createRootPartitionTableEntry(disk)
        progress_callback(30)
        backend.createDom0DiskFilesystems(disk)
        progress_callback(40)
        backend.transferFSfromBackupToRoot(disk)
        progress_callback(50)
        backend.removeBackupPartition(disk)
        progress_callback(60)
        backend.createBackupPartition(disk)
        progress_callback(70)
        backend.extractOemStatefromRootToBackup(existing)
        progress_callback(80)

        return inst_ctrl_pd

################################################################################

# Upgraders provided here, in preference order:
class XUpgraderList(list):
    def getUpgrader(self, product, variant_class, version):
        for x in self:
            if x.upgrades(product, variant_class, version):
                return x
        raise KeyError, "No upgrader found for %s" % version

    def hasUpgrader(self, product, variant_class, version):
        for x in self:
            if x.upgrades(product, variant_class, version):
                return True
        return False

class UpgraderList(list):
    def getUpgrader(self, product, version, variant):
        for x in self:
            if x.upgrades(product, version, variant):
                return x
        raise KeyError, "No upgrader found for %s" % version

    def hasUpgrader(self, product, version, variant):
        for x in self:
            if x.upgrades(product, version, variant):
                return True
        return False
    
__upgraders__ = UpgraderList([ ThirdGenUpgrader, ThirdGenOEMUpgrader ])

def filter_for_upgradeable_products(installed_products):
    upgradeable_products = filter(lambda p: p.isUpgradeable() and upgradeAvailable(p),
        installed_products)
    return upgradeable_products
