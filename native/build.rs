use glob::glob;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fmt::Write as _;
use std::fs;
use std::path::{Component, Path, PathBuf};

const INVENTORY_FILE: &str = "source-inventory-v1.txt";
const INVENTORY_HEADER: &str = "clusy-native-source-inventory-v1";
const DIGEST_DOMAIN: &[u8] = b"clusy-native-source-digest-v1\0";

fn inventory_patterns(root: &Path) -> Vec<String> {
    let inventory_path = root.join(INVENTORY_FILE);
    let inventory = fs::read_to_string(&inventory_path)
        .unwrap_or_else(|error| panic!("could not read {INVENTORY_FILE}: {error}"));
    let mut lines = inventory.lines();
    let header = lines
        .next()
        .unwrap_or_else(|| panic!("{INVENTORY_FILE} is empty"));
    assert_eq!(
        header, INVENTORY_HEADER,
        "{INVENTORY_FILE} has an unsupported header"
    );

    let mut patterns = Vec::new();
    for (offset, raw_pattern) in lines.enumerate() {
        let line_number = offset + 2;
        let pattern = raw_pattern.trim();
        if pattern.is_empty() || pattern.starts_with('#') {
            continue;
        }
        let path = Path::new(pattern);
        let unsafe_component = path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        });
        assert!(
            !unsafe_component && !pattern.contains('\\'),
            "{INVENTORY_FILE}:{line_number} has an unsafe non-POSIX pattern: {pattern}"
        );
        assert!(
            !patterns.iter().any(|value| value == pattern),
            "{INVENTORY_FILE}:{line_number} repeats pattern: {pattern}"
        );
        patterns.push(pattern.to_owned());
    }
    assert!(
        !patterns.is_empty(),
        "{INVENTORY_FILE} contains no source patterns"
    );
    patterns
}

fn source_files(root: &Path, patterns: &[String]) -> BTreeMap<String, PathBuf> {
    let mut files = BTreeMap::new();
    for pattern in patterns {
        let absolute_pattern = root.join(pattern);
        let absolute_pattern = absolute_pattern
            .to_str()
            .unwrap_or_else(|| panic!("native source glob is not UTF-8: {pattern}"));
        let mut matches = 0_usize;
        let entries = glob(absolute_pattern)
            .unwrap_or_else(|error| panic!("invalid source glob {pattern}: {error}"));
        for entry in entries {
            let path = entry.unwrap_or_else(|error| {
                panic!("could not evaluate source glob {pattern}: {error}")
            });
            let mut cursor = path.as_path();
            while cursor != root {
                let metadata = fs::symlink_metadata(cursor).unwrap_or_else(|error| {
                    panic!(
                        "could not stat native source component {}: {error}",
                        cursor.display()
                    )
                });
                assert!(
                    !metadata.file_type().is_symlink(),
                    "native source inventory contains a symbolic link: {}",
                    cursor.display()
                );
                cursor = cursor.parent().unwrap_or_else(|| {
                    panic!("native source escaped package root: {}", path.display())
                });
            }
            let metadata = fs::symlink_metadata(&path).unwrap_or_else(|error| {
                panic!("could not stat native source {}: {error}", path.display())
            });
            assert!(
                metadata.is_file(),
                "native source inventory entry is not a regular file: {}",
                path.display()
            );
            let relative = path.strip_prefix(root).unwrap_or_else(|_| {
                panic!("native source escaped package root: {}", path.display())
            });
            let relative = relative
                .to_str()
                .unwrap_or_else(|| panic!("native source path is not UTF-8: {}", path.display()))
                .replace('\\', "/");
            assert!(
                files.insert(relative.clone(), path).is_none(),
                "native source file {relative} is matched by more than one inventory pattern"
            );
            matches += 1;
        }
        assert!(
            matches > 0,
            "native source inventory pattern matched no files: {pattern}"
        );
    }
    assert!(!files.is_empty(), "native source inventory is empty");
    files
}

fn update_frame(hasher: &mut Sha256, value: &[u8]) {
    let length = u64::try_from(value.len()).expect("native source frame exceeds u64");
    hasher.update(length.to_be_bytes());
    hasher.update(value);
}

fn source_digest(files: &BTreeMap<String, PathBuf>) -> String {
    let mut hasher = Sha256::new();
    hasher.update(DIGEST_DOMAIN);
    hasher.update(
        u64::try_from(files.len())
            .expect("native source inventory exceeds u64")
            .to_be_bytes(),
    );
    for (relative, path) in files {
        update_frame(&mut hasher, relative.as_bytes());
        let data = fs::read(path)
            .unwrap_or_else(|error| panic!("could not read native source {relative}: {error}"));
        update_frame(&mut hasher, &data);
    }

    let mut digest = String::with_capacity(64);
    for byte in hasher.finalize() {
        write!(&mut digest, "{byte:02x}").expect("writing to a String cannot fail");
    }
    digest
}

fn emit_rerun_paths(root: &Path, files: &BTreeMap<String, PathBuf>) {
    let mut directories = BTreeSet::new();
    for (relative, path) in files {
        println!("cargo:rerun-if-changed={relative}");
        let mut parent = path.parent();
        while let Some(directory) = parent {
            if directory == root {
                break;
            }
            let relative_directory = directory
                .strip_prefix(root)
                .expect("native source parent escaped package root");
            directories.insert(relative_directory.to_path_buf());
            parent = directory.parent();
        }
    }
    for directory in directories {
        println!("cargo:rerun-if-changed={}", directory.display());
    }
}

fn main() {
    let root = PathBuf::from(
        env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is not defined"),
    );
    let patterns = inventory_patterns(&root);
    let files = source_files(&root, &patterns);
    let digest = source_digest(&files);
    emit_rerun_paths(&root, &files);
    println!("cargo:rustc-env=CLUSY_NATIVE_SOURCE_DIGEST={digest}");
}
