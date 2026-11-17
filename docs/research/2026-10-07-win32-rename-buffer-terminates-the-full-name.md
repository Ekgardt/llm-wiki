# The Win32 rename buffer terminates the full name

Research date: 2026-10-07. This changes a test fixture, not the installed reader or runtime contracts.

The native Windows job at 4c28e8c5 returned success from SetFileInformationByHandle but observed a final source-handle name with additional suffix characters. The requested target retained its old physical identity. The fixture allocated exactly the UTF-16 name bytes after the FILE_RENAME_INFO header, with no room for a terminating WCHAR. Two causal tests reproduce that missing storage on the unchanged helper, for ASCII and supplementary Unicode paths.

Three independent primary sources inform the correction:

- [Microsoft FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info): FileNameLength is the byte length; FileName is described as a NUL-terminated wide string. The member descriptions are inconsistent about whether a terminator is required. Supplying one satisfies both without including it in FileNameLength.
- [Wine's Win32 implementation](https://github.com/wine-mirror/wine/blob/master/dlls/kernelbase/file.c): SetFileInformationByHandle converts FileName through RtlDosPathNameToNtPathName_U_WithStatus before constructing the native rename record. This independently illustrates why the Win32 wrapper can consume a terminated string rather than only its declared byte length. Wine does not prove Microsoft's implementation.
- [CPython ctypes documentation](https://docs.python.org/3/library/ctypes.html#ctypes.create_string_buffer): integer-sized buffers are zero-initialized, but bytes copied into all available storage leave no terminating character. A UTF-16 terminator needs two bytes independently of the host's c_wchar width.

The chosen correction reserves one additional c_uint16 after the complete encoded path. The zero-initialized terminator stays outside FileNameLength. Flags, access, sharing, handle lifetime and all original identity/security refusal assertions remain unchanged. UTF-16 supplementary characters retain their original encoded bytes.

Reject closing the held reader, accepting rename failure or relaxing snapshot assertions: none exercises the intended replacement race. Direct NtSetInformationFile would introduce a different API and unnecessary fixture complexity. Adding terminator storage addresses the observed buffer defect at its shared producer.

Linux regression results establish the buffer correction only. Real Windows replacement, handle identity and full provider qualification must pass on the exact successor commit before installation. A successful native API return alone is insufficient evidence.
