# Repository signing keys

Public keys for the third-party repositories a profile declares under
`software.repositories`, referenced by path (`key: keys/<name>.asc`).
ASCII-armored; apt reads them directly through `Signed-By`.

`example-acme.asc` is a throwaway key generated for the example profile.
It signs nothing real. Replace it with your vendor's published key and
verify the fingerprint out of band before committing it.
