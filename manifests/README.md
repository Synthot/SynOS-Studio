# Manifests

`../manifest.yml` is the default build. Additional manifests live here and are
selected with `make MANIFEST=manifests/<name>.yml`. Customers keep their own
manifests, profiles and brand kits in their own repository and point the
build at them the same way.
