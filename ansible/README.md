# Ansible

`collections/ansible_collections/synos/workstation` is the collection;
see its README for the roles. Two entry points:

```bash
# build an image on your own runner
ansible-playbook ansible/collections/ansible_collections/synos/workstation/playbooks/build.yml \
    -e manifest=manifests/acme.yml

# apply the roles to installed machines
ansible-playbook -i ansible/inventory/hosts.yml site.yml
```

`build.sh` runs `playbooks/customize_chroot.yml` inside the chroot, followed
by any playbooks the profile lists under `ansible.playbooks` (paths relative
to the repository), using the `community.general.chroot` connection.
