//! `NSPasteboard` implementation of [`Pasteboard`](super::pasteboard::Pasteboard).
//!
//! Reads every item's every type as raw `NSData`, and writes them back as
//! `NSPasteboardItem`s so multi-item, multi-type clipboards (rich text,
//! images, file URLs, app-private types) round-trip byte-for-byte.

use objc2::rc::Retained;
use objc2::runtime::ProtocolObject;
use objc2_app_kit::{NSPasteboard, NSPasteboardItem, NSPasteboardWriting};
use objc2_foundation::{NSArray, NSData, NSString};

use super::pasteboard::{ItemSnapshot, Pasteboard, Snapshot, SESSION_MARKER_TYPE};

pub struct MacPasteboard {
    pasteboard: Retained<NSPasteboard>,
}

impl MacPasteboard {
    pub fn general() -> Self {
        Self {
            pasteboard: NSPasteboard::generalPasteboard(),
        }
    }
}

impl Pasteboard for MacPasteboard {
    fn change_count(&self) -> i64 {
        self.pasteboard.changeCount() as i64
    }

    fn snapshot(&self) -> Snapshot {
        let mut items = Vec::new();
        let Some(pasteboard_items) = self.pasteboard.pasteboardItems() else {
            return Snapshot { items };
        };
        for item in pasteboard_items.iter() {
            let mut representations = Vec::new();
            for data_type in item.types().iter() {
                // A promised type whose provider has gone away yields nil;
                // there is nothing to restore for it, so skip it.
                if let Some(data) = item.dataForType(&data_type) {
                    representations.push((data_type.to_string(), data.to_vec()));
                }
            }
            items.push(ItemSnapshot { representations });
        }
        Snapshot { items }
    }

    fn write_items(&self, items: &[ItemSnapshot]) -> i64 {
        self.pasteboard.clearContents();
        let objects: Vec<Retained<ProtocolObject<dyn NSPasteboardWriting>>> = items
            .iter()
            .filter(|item| !item.representations.is_empty())
            .map(|item| {
                let pasteboard_item = NSPasteboardItem::new();
                for (data_type, bytes) in &item.representations {
                    let data = NSData::with_bytes(bytes);
                    let data_type = NSString::from_str(data_type);
                    pasteboard_item.setData_forType(&data, &data_type);
                }
                ProtocolObject::from_retained(pasteboard_item)
            })
            .collect();
        if !objects.is_empty() {
            let objects = NSArray::from_retained_slice(&objects);
            if !self.pasteboard.writeObjects(&objects) {
                log::warn!("[pasteboard] writeObjects failed while restoring the clipboard");
            }
        }
        self.pasteboard.changeCount() as i64
    }

    fn session_marker(&self) -> Option<String> {
        let marker_type = NSString::from_str(SESSION_MARKER_TYPE);
        self.pasteboard
            .stringForType(&marker_type)
            .map(|s| s.to_string())
    }
}
