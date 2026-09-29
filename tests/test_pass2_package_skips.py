"""P2-09: files inside packages the scan did not recognise were offered for deletion.

The duplicates scan skips macOS packages (folders that the Finder shows as a
single document) by extension, but the list missed common ones: VMware,
Parallels and UTM virtual machines, sparse bundles, Scrivener and iWork
packages, Lightroom and Capture One libraries, migrated iPhoto libraries,
plug-ins and installers. Their internal files were grouped as duplicates, and
"Delete all redundant" deleted them, which breaks the package. For example,
the unused extents of a split VMware disk can be byte-identical, so all but
one of them were deleted.

Bundles with extensions not in any list are now recognised by their layout
(Contents/Info.plist).
"""

import os
import unittest

from pass2_helpers import Pass2Case, payload, write_file

# One entry per package type added by the fix: (package folder, file inside).
PACKAGES = [
    ("VMs/Windows 11.vmwarevm", "Virtual Disk-s002.vmdk"),
    ("VMs/Ubuntu.utm", "Data/disk.qcow2"),
    ("VMs/Windows.pvm", "harddisk.hdd/harddisk.hds"),
    ("Backups/Mac.sparsebundle", "bands/1f"),
    ("Backups/Mac.backupbundle", "bands/2a"),
    ("Writing/Novel.scriv", "Files/Data/4C1D/content.rtf"),
    ("Work/Report.pages", "Data/photo.jpg"),
    ("Work/Budget.numbers", "Data/logo.png"),
    ("Work/Talk.key", "Data/slide.jpg"),
    ("Pictures/Lightroom Library.lrlibrary", "originals/2021/IMG_0001.JPG"),
    ("Pictures/Catalog Previews.lrdata", "0/01/preview.lrprev"),
    ("Pictures/Capture One.cocatalog", "Originals/IMG_0002.JPG"),
    ("Pictures/iPhoto Library.migratedphotolibrary", "Masters/2012/IMG_0003.JPG"),
    ("Audio/Synth.vst3", "Contents/Resources/preset.bin"),
    ("Audio/Synth.component", "Contents/Resources/preset.bin"),
    ("Installers/Tool.pkg", "Contents/Archive.pax.gz"),
]


class PackageSkipTest(Pass2Case):

    def inside(self, path, package):
        return path.startswith(self.path(package) + os.sep)

    def offered(self, groups):
        return [p for g in groups for p in g["files"]]

    def test_files_inside_packages_are_not_offered(self):
        for n, (package, inner) in enumerate(PACKAGES):
            data = payload(f"p2-09 {n}", 40_000 + n)
            write_file(self.path(package, inner), data)
            write_file(self.path("Loose", f"{n:02d}", os.path.basename(inner)), data)
            write_file(self.path("Loose copy", f"{n:02d}", os.path.basename(inner)), data)
        offered = self.offered(self.scan())
        for package, _inner in PACKAGES:
            with self.subTest(package=package):
                self.assertEqual([p for p in offered if self.inside(p, package)], [])
        # The loose copies are still found.
        self.assertEqual(len(offered), 2 * len(PACKAGES))

    def test_split_vmware_disk_keeps_all_its_extents(self):
        vm = self.path("VMs", "Windows 11.vmwarevm")
        write_file(os.path.join(vm, "Windows 11.vmx"), b'displayName = "Windows 11"\n')
        write_file(os.path.join(vm, "Virtual Disk-s001.vmdk"), payload("p2-09 used", 90_000))
        unused = payload("p2-09 unused extent", 70_000)
        extents = [
            write_file(os.path.join(vm, f"Virtual Disk-s{i:03d}.vmdk"), unused)
            for i in range(2, 6)
        ]
        api = self.api()
        groups = api.find_duplicates_inplace(self.root)
        redundant = [p for g in groups for p in g["files"][1:]]
        api.trash_inplace_duplicates(redundant, self.root, True, groups)
        self.assertEqual([p for p in extents if not os.path.exists(p)], [])

    def test_unlisted_bundle_is_recognised_by_its_layout(self):
        bundle = self.path("Plug-Ins", "Reverb.clap")
        write_file(os.path.join(bundle, "Contents", "Info.plist"), b"<plist/>")
        data = payload("p2-09 bundle", 30_000)
        write_file(os.path.join(bundle, "Contents", "Resources", "a.bin"), data)
        write_file(os.path.join(bundle, "Contents", "Resources", "b.bin"), data)
        self.assertEqual(self.scan(), [])

    def test_ordinary_folders_are_still_scanned(self):
        # Control: passes before and after the fix. A folder that merely has
        # a "Contents" subfolder, or a name like a package's, is not a bundle
        # unless it has the bundle layout or a listed extension.
        data = payload("p2-09 control", 30_000)
        a = write_file(self.path("Book", "Contents", "chapter1.txt"), data)
        b = write_file(self.path("Book", "Drafts", "chapter1.txt"), data)
        c = write_file(self.path("Old Drive.hdd", "chapter1.txt"), data)
        offered = sorted(self.offered(self.scan()))
        self.assertEqual(offered, sorted([a, b, c]))

    def test_scanning_a_package_directly_still_looks_inside(self):
        # Control: passes before and after the fix. Pointing the utility at a
        # package itself is a deliberate choice, as before.
        vm = self.path("Windows 11.vmwarevm")
        data = payload("p2-09 direct", 30_000)
        write_file(os.path.join(vm, "a.log"), data)
        write_file(os.path.join(vm, "old", "a.log"), data)
        groups = self.api().find_duplicates_inplace(vm)
        self.assertEqual(len(groups), 1)


if __name__ == "__main__":
    unittest.main()
