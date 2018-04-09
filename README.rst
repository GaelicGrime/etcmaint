**etcmaint [--version] {help,create,diff,sync,update} ...**

An Arch Linux tool that uses git for the maintenance of /etc files.

The /etc files installed or upgraded by Arch Linux packages are managed in the
``etc`` branch of a git repository. The /etc files customized or created by
the user are managed in the ``master`` branch. The upgraded package changes of
the user-customized files are merged (actually cherry-picked) with the
``update`` subcommand. Merge conflicts must be resolved and commited by the
user. After a merge, the ``sync`` subcommand is used to retrofit the merged
changes to /etc.

Run ``etcmaint help <subcommand>`` to get help on a subcommand.
