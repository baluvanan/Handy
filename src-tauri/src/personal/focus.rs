//! What has keyboard focus when a dictation ends: the frontmost app's bundle
//! id (for logging and per-app profiles) and whether the focused field is a
//! secure (password) field, which must never be pasted into silently.

/// The frontmost application's bundle identifier (macOS), e.g.
/// `com.tinyspeck.slackmacgap`. `None` elsewhere or when unknown.
#[allow(dead_code)] // TEMP: wired up by dataset logging
pub fn frontmost_app_id() -> Option<String> {
    imp::frontmost_app_id()
}

/// True when the focused UI element is a secure text field, or when macOS
/// Secure Event Input is on (password fields enable it; so does Terminal's
/// "Secure Keyboard Entry"). Always false off macOS.
pub fn secure_field_focused() -> bool {
    imp::secure_field_focused()
}

#[cfg(target_os = "macos")]
mod imp {
    use std::ffi::c_void;

    use objc2::rc::Retained;
    use objc2_app_kit::NSWorkspace;
    use objc2_foundation::NSString;

    type AXUIElementRef = *const c_void;
    type CFTypeRef = *const c_void;
    /// `kAXErrorSuccess`.
    const AX_SUCCESS: i32 = 0;

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXUIElementCreateSystemWide() -> AXUIElementRef;
        fn AXUIElementCopyAttributeValue(
            element: AXUIElementRef,
            attribute: CFTypeRef,
            value: *mut CFTypeRef,
        ) -> i32;
        fn AXIsProcessTrusted() -> bool;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(cf: CFTypeRef);
    }

    pub fn frontmost_app_id() -> Option<String> {
        let workspace = NSWorkspace::sharedWorkspace();
        let app = workspace.frontmostApplication()?;
        app.bundleIdentifier().map(|id| id.to_string())
    }

    /// Copies an attribute of an AX element. The caller owns the result.
    unsafe fn copy_attribute(element: AXUIElementRef, name: &str) -> Option<CFTypeRef> {
        // NSString is toll-free bridged with CFString.
        let attribute = NSString::from_str(name);
        let mut value: CFTypeRef = std::ptr::null();
        let err = AXUIElementCopyAttributeValue(
            element,
            Retained::as_ptr(&attribute) as CFTypeRef,
            &mut value,
        );
        (err == AX_SUCCESS && !value.is_null()).then_some(value)
    }

    /// Reads a string attribute (role/subrole) of an AX element.
    unsafe fn string_attribute(element: AXUIElementRef, name: &str) -> Option<String> {
        let value = copy_attribute(element, name)?;
        // Role and subrole are CFStrings; take ownership of the +1 copy so it
        // is released when dropped.
        let string = Retained::from_raw(value as *mut NSString)?;
        Some(string.to_string())
    }

    fn focused_element_is_secure() -> bool {
        // Without Accessibility trust every AX query fails; Handy needs that
        // permission to paste anyway, so this only matters before it's granted.
        if !unsafe { AXIsProcessTrusted() } {
            return false;
        }
        unsafe {
            let system = AXUIElementCreateSystemWide();
            if system.is_null() {
                return false;
            }
            let focused = copy_attribute(system, "AXFocusedUIElement");
            CFRelease(system);
            let Some(focused) = focused else {
                return false;
            };
            let subrole = string_attribute(focused, "AXSubrole");
            let role = string_attribute(focused, "AXRole");
            CFRelease(focused);
            subrole.as_deref() == Some("AXSecureTextField")
                || role.as_deref() == Some("AXSecureTextField")
        }
    }

    pub fn secure_field_focused() -> bool {
        focused_element_is_secure() || crate::secure_input::is_enabled_now()
    }
}

#[cfg(not(target_os = "macos"))]
mod imp {
    #[allow(dead_code)] // TEMP: wired up by dataset logging
    pub fn frontmost_app_id() -> Option<String> {
        None
    }

    pub fn secure_field_focused() -> bool {
        false
    }
}
