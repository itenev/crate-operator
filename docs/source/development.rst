Working on the operator
=======================

Local installation
------------------

The ``crate-operator`` package requires **Python 3.12**.

It is recommended to use a virtual environment to the operator and its
dependencies for local development.

.. note::

   If you are using macOS, you need to have PostgreSQL installed. You can
   install it via Homebrew_ (``brew install postgresql``) or follow the
   `PostgreSQL docs`_.

.. code-block:: console

   $ python3.12 -m venv env
   $ source env/bin/activate
   (env)$ python -m pip install -e .

Local patched image (Hive)
--------------------------

For a local candidate based on the reviewed 2.63.0 patch, use Python package
version ``2.63.0+hive.1`` and image tag ``2.63.0-hive.1``. The version override
is required for a linked worktree: its ``.git`` pointer does not resolve inside
the Docker build context. Normal upstream builds may omit this override and
continue to derive the version from their full Git checkout.

Resolve and record the base digest before building:

.. code-block:: console

   $ docker buildx imagetools inspect python:3.12-slim
   $ export PYTHON_IMAGE=python:3.12-slim@sha256:<resolved-index-digest>

Stage the reviewed build changes first. The patch checksum covers the complete
tracked and newly staged diff against HEAD, not arbitrary untracked files.
Check ``git status --short`` and keep unrelated files out of the build context.
For this uncommitted candidate:

.. code-block:: console

   $ export SOURCE_REVISION=$(git rev-parse HEAD)
   $ export SOURCE_PATCH_SHA256=$(git diff HEAD --binary | sha256sum | cut -d' ' -f1)
   $ docker build --platform linux/amd64 --progress plain \
       --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
       --build-arg OPERATOR_VERSION=2.63.0+hive.1 \
       --build-arg SOURCE_URL=https://github.com/itenev/crate-operator \
       --build-arg SOURCE_REVISION="$SOURCE_REVISION" \
       --build-arg SOURCE_STATE=dirty \
       --build-arg SOURCE_PATCH_SHA256="$SOURCE_PATCH_SHA256" \
       -t hive/crate-operator:2.63.0-hive.1 .

If Docker bridge DNS is unavailable locally, diagnose that separately and use
``--network=host`` for this build only; do not change runtime networking or
mount credentials/kubeconfig. This flag is a local workaround, not required by
the image.

Using the development virtual environment with ``.[testing]`` installed:

.. code-block:: console

   $ CRATE_OPERATOR_TEST_IMAGE=hive/crate-operator:2.63.0-hive.1 \
       python -m pytest tests/test_local_image_build.py -q
   $ python -m pytest -q
   $ pre-commit run
   $ docker image inspect hive/crate-operator:2.63.0-hive.1
   $ docker run --rm --network=none --entrypoint python \
       hive/crate-operator:2.63.0-hive.1 -m pip list --format=json
   $ docker run --rm --network=none --entrypoint dpkg-query \
       hive/crate-operator:2.63.0-hive.1 -W

Image tests use no network or Kubernetes credentials. They verify installed
package behavior, non-root execution, license retention, provenance metadata
and executable/module loading, not live reconciliation. The source includes
``LICENSE`` and no separate NOTICE file. The image retains that license.

Record the source revision, full staged patch/checksum, base digest, image ID,
platform and package inventories with the build results. Apt/Python package
indexes are still live; this records resolved inputs but does not guarantee
bit-for-bit rebuilds. The local candidate is amd64 only. Before distribution,
commit/review the build changes, rebuild from a clean source, use accurate
``SOURCE_STATE=clean`` metadata and determine required deployment architectures,
registry, scanning and promotion policy.

For a clean committed build, stream a Git archive rather than the worktree
directory. This excludes untracked downloads and local test artifacts. Set
``SOURCE_REVISION`` to the reviewed commit, keep the pinned ``PYTHON_IMAGE``,
and use a different tag so the dirty candidate remains identifiable:

.. code-block:: console

   $ export SOURCE_REVISION=$(git rev-parse HEAD)
   $ set -o pipefail
   $ git archive "$SOURCE_REVISION" | docker build --platform linux/amd64 \
       --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
       --build-arg OPERATOR_VERSION=2.63.0+hive.1 \
       --build-arg SOURCE_URL=https://github.com/itenev/crate-operator \
       --build-arg SOURCE_REVISION="$SOURCE_REVISION" \
       --build-arg SOURCE_STATE=clean \
       -t hive/crate-operator:2.63.0-hive.1-clean -
   $ CRATE_OPERATOR_TEST_IMAGE=hive/crate-operator:2.63.0-hive.1-clean \
       CRATE_OPERATOR_TEST_SOURCE_STATE=clean \
       CRATE_OPERATOR_TEST_REVISION="$SOURCE_REVISION" \
       python -m pytest tests/test_local_image_build.py -q

Clean builds must have an empty patch-checksum label. The image tests default
to expecting dirty source and a hexadecimal SHA256 patch checksum; explicitly
select ``clean`` for an archive build. ``CRATE_OPERATOR_TEST_REVISION`` checks
the exact expected commit when supplied. These checks still do not prove
Kubernetes reconciliation or authorize image publication.

Local ClusterIP integration validation
-------------------------------------

``tests/test_clusterip_integration.py`` is deliberately destructive and opt-in.
It targets only the prepared Docker-driver Minikube context
``crate-clusterip`` and checks the node's Minikube profile label before writes.
Use a separate kubeconfig, not a production context renamed to pass the check.

Prepare Kubernetes 1.34 with 8 CPUs and 20 GiB RAM, a working StorageClass
literally named ``default`` using ``k8s.io/minikube-hostpath``, and the patched
CRD. Minikube's normally supplied class named ``standard`` is not sufficient
for the upstream test defaults. Do not enable Traefik or a LoadBalancer tunnel
for these internal-only lifecycle checks.

Load the clean image into that profile and install the operator in namespace
``crate-operator`` using that image with ``imagePullPolicy: Never``.
Leave ``CRATEDB_OPERATOR_TESTING`` unset: the host-side test mode assumes
external addressing. Set debug-volume size to ``2GiB`` and its class to
``default``. Create namespace ``crate-validation`` and CrateDB
``clusterip-smoke`` with the following spec:

.. code-block:: yaml

   cluster:
     name: clusterip-smoke
     imageRegistry: crate
     version: "5.6.5"
     exposure: clusterip
   nodes:
     data:
       - name: hot
         replicas: 1
         resources:
           requests: {cpu: 1, memory: 2Gi}
           limits: {cpu: 2, memory: 4Gi}
           heapRatio: 0.25
           disk: {storageClass: default, size: 4GiB, count: 1}

Wait for completed bootstrap before running:

.. code-block:: console

   $ CRATE_OPERATOR_TEST_CLUSTERIP=true python -m pytest \
       tests/test_clusterip_integration.py \
       --kube-config=/path/to/dedicated/kubeconfig \
       --kube-context=crate-clusterip -q

The checks query SQL from inside the operator Pod using its existing Secret,
without printing credentials. They suspend/resume the smoke cluster (including
client Service recreation), exercise a compute-triggered restart and create
short-lived rejection fixtures. They change the smoke cluster's CPU limit
between 2 and 3. Wait for an update's completed Kopf state before requesting
the next edit, not merely for the Pod count to change. Kopf can remove
permanent-error progress annotations: rejection checks use UID-scoped Events.

These tests do not install their prerequisites or clean up the smoke cluster.
After recording results, delete its CR while the operator is running and wait
for garbage collection of its owned Pods and PVCs. Inspect and delete any
remaining test PVCs before removing the test namespaces. Verify PV reclamation
before stopping the dedicated profile. Never delete unrelated profiles or
resources. Keep the clean-source image and command/package records identifiable.

The broader upstream Kubernetes suite additionally needs external-address
allocation/access, routing CRDs/controllers, expandable storage or external
backup credentials depending on the test. Record exact selected and excluded
cases; a local ClusterIP pass is not a claim that the entire suite ran or that
AWS/OpenStack behavior has been verified.

Contributing
------------

Before we can accept any pull requests, we need you to agree to our CLA_.

Once that is complete, you should:

- Create an issue on GitHub to let us know that you're working on the issue.

- Use a feature branch and not ``master``.

- Rebase your feature branch onto the upstream ``master`` before creating the
  pull request.

- `Be descriptive in your PR and commit messages
  <#meaningful-commit-messages>`_. What is it for? Why is it needed? And so on.

- If applicable, `run the tests <#testing>`_.

- Squash related commits.


.. _testing:

Testing
-------

Testing the CrateDB Kubernetes Operator is possible at two levels. There are
unit tests that run without a Kubernetes cluster, and there are integration and
end-to-end style tests that do require a Kubernetes cluster to run.

To run either set of tests, one needs to first install the operator and its
test dependencies. This is typically done inside a Python virtual environment:

.. code-block:: console

   $ python3.12 -m venv env
   $ source env/bin/activate
   (env)$ python -m pip install -e '.[testing]'
   Successfully installed ... crate-operator ...

Now, running the unit test can be done by using pytest_ from within the
activated virtual environment:

.. code-block:: console

   (env)$ pytest

Running the integration or end-to-end style tests requires a running Kubernetes
cluster and access to it through ``kubectl``. That also means you need to have
a ``kubeconfig`` file at hand. Often enough, you can find that file in the
``.kube`` folder in your home directory (``$HOME/.kube/config``).

.. warning::

   Before you run the tests, make sure you're not using a production Kubernetes
   cluster, as some tests will remove resources. This can easily interfere with
   your production operation!

To reduce the risk of accidentally using a production Kubernetes context, the
operator requires not only specifying the path to the ``kubeconfig``, but
additionally the Kubernetes context to use. These two parameters need to be
passed via the ``--kube-config`` and ``--kube-context`` arguments to pytest_.
Furthermore, the context *must* start with either ``crate-`` or be called
``minikube``.

Running with a local k8s cluster
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``microk8s``, ``kind`` and ``Minikube`` have all been proven to work, however there are
some pre-requisites:

- You **must** have enough CPU and memory to run the tests: Some of the upgrade tests
  provision a 3-node cluster with a reservation of 2 CPU cores and 4GiB of RAM each.
  ``kubectl get nodes`` and ``kubectl describe node [your node]`` to check.

- You **must** have a k8s Load Balancer implementation that gives out IP addresses,
  usually this is MetalLB.

- You **must** have a StorageClass called ``default`` that actually works and provisions
  volumes.

Minikube
^^^^^^^^

Using minikube_ as Kubernetes backend for the tests requires a few
preparations. For example, make sure your minikube has configured enough
resources for example:

.. code-block:: console

   $ minikube config view
   - cpus: 4
   - disk-size: 64G
   - memory: 8192

Make sure you have a storage class named ``default``, which uses the
``k8s.io/minikube-hostpath`` provisioner:

.. code-block:: yaml

   apiVersion: storage.k8s.io/v1
   kind: StorageClass
   metadata:
     name: default
   provisioner: k8s.io/minikube-hostpath
   reclaimPolicy: Delete
   volumeBindingMode: Immediate

Additionally, you need to set up a `minikube tunnel`_ in order to get
an external IP address assigned to the CrateDB services.

.. code-block:: console

   $ minikube tunnel

Finally, you can run ``pytest`` using the required arguments:

.. code-block:: console

   (env)$ pytest -vv --kube-config=~/.kube/test_config --kube-context=crate-testing


.. _pytest: https://docs.pytest.org/en/latest/
.. _minikube: https://minikube.sigs.k8s.io
.. _minikube tunnel: https://minikube.sigs.k8s.io/docs/handbook/accessing/#using-minikube-tunnel


Running test in parallel
^^^^^^^^^^^^^^^^^^^^^^^^

The operator supports running the entire test suite in parallel, to significantly speed
up execution. To do that, simply run the tests with the ``-n X`` parameter to pytest:

.. code-block:: console

   (env)$ pytest -n 2 --dist loadfile -vv --kube-config=~/.kube/test_config --kube-context=crate-testing

Code style
----------

This project uses `pre-commit`_ to ensure proper linting, code formatting, and
type checking. Tools, such as ``black``, ``flake8``, ``isort``, and ``mypy``
should be run as hooks upon committing or pushing code. When at least one of
the hooks fails, committing or pushing changes is aborted and manual
intervention is necessary. For example, an ill-formatted piece of Python code
that is staged for committing with Git, would be cleaned up by the ``black``
hook. It's then up to the developer to either amend the changes or stage them
as well.

Install ``pre-commit`` for your user and verify that the installation worked:

.. code-block:: console

   $ pip install --user pre-commit
   $ pre-commit --version
   pre-commit 2.4.0

Please keep in mind that the version shown above might vary.

Once you've confirmed the successful installation of ``pre-commit``, install
the hooks for this project:

.. code-block:: console

   $ pre-commit install -t pre-commit -t pre-push --install-hooks
   pre-commit installed at .git/hooks/pre-commit
   pre-commit installed at .git/hooks/pre-push
   [INFO] Installing environment for

From now on, each time you run ``git commit`` or ``git push``, ``pre-commit``
will run "hooks" defined in the ``.pre-commit-config.yaml`` file on the staged
files.


.. _pre-commit: https://pre-commit.com/


Making a release
----------------

``crate-operator`` uses `setuptools-scm`_. That means, bumping the package's
version happens automatically for each git commit or git tag. The operator's
versions follows Python's :pep:`440` format, where the first 3 parts represent
the *major*, *minor*, and *patch* parts according to `Semantic
Versioning`_.

For the following steps we assume the next version is going to be ``$VERSION``.

#. When ready to prepare a new release, start a new branch ``release/$VERSION``:

   .. code-block:: console

      $ git checkout -b "release/$VERSION"

#. Next, go ahead and ensure the changelog ``CHANGES.rst`` is up to date.

#. Run ``./devtools/bumpversion.sh $VERSION`` to update version:

        $ ./devtools/bumpversion.sh 2.14.1
         INFO:  Current branch is 'release/2.14.1'.
         INFO:  Incrementing crate-operator Helm Chart to version '2.14.1'
         INFO:  Incrementing crate-operator-crds Helm Chart to version '2.14.1'
         INFO:  Done. ✨

   If you want to inspect the changes made, run ``git diff``.

#. Commit the changes to the ``CHANGES.rst``,
   ``deploy/charts/crate-operator/Chart.yaml`` and
   ``deploy/charts/crate-operator-crds/Chart.yaml``, push them to GitHub, and open a
   pull request against the ``master`` branch:

   .. code-block:: console

      $ git add CHANGES.rst deploy/charts/crate-operator/Chart.yaml deploy/charts/crate-operator-crds/Chart.yaml
      $ git commit -m "Prepare release $VERSION"
      $ git push --set-upstream origin "release/$VERSION"

#. After merging the pull request to the ``master`` branch, fetch the latest
   changes and create the release:

   .. code-block:: console

      $ git checkout master
      $ git pull
      $ ./devtools/create_tag.sh "$VERSION"


Automatic Helm chart release
----------------------------

This GitHub repo uses the ``chart-releaser`` action that detects any
modification in the repo's charts during every push to ``master``.
The action creates the artifacts and the corresponding GitHub release, then
it updates the index.yaml in ``gh-pages`` branch, which is hosted on GitHub Pages.

For more details, see `Helm Chart Releaser`_.


.. _Helm Chart Releaser: https://github.com/marketplace/actions/helm-chart-releaser


General Tips
------------

.. _commit-message-style:

Meaningful Commit Messages
^^^^^^^^^^^^^^^^^^^^^^^^^^

Please choose a meaningful commit message. The commit message is not only
valuable during the review process, but can be helpful for reasoning about any
changes in the code base. For example, PyCharm's "Annotate" feature, brings up
the commits which introduced the code in a source file. Without meaningful
commit messages, the commit history does not provide any valuable information.

The first line of the commit message (also known as "subject line") should
contain a summary of the changes. Please use the imperative mood. The subject
can be prefixed with "Test: " or "Docs: " to indicate the changes are not
primarily to the main code base. For example::

   Put a timeout on all bootstrap operations
   Test: Increase bootstrap timeout in tests
   Docs: Copyedit docs on configuration options

See also: https://chris.beams.io/posts/git-commit/

Updating Your Branch
^^^^^^^^^^^^^^^^^^^^

If new commits have been added to the upstream ``master`` branch since you
created your feature branch, please do not merge them in to your branch.
Instead, rebase your branch::

   $ git fetch upstream
   $ git rebase upstream/master

This will apply all commits on your feature branch on top of the upstream
``master`` branch. If there are conflicts, they can be resolved with ``git
merge``. After the conflict has been resolved, use ``git rebase --continue`` to
continue the rebase process.

Squashing Minor Commits
^^^^^^^^^^^^^^^^^^^^^^^

Minor commits that only fix typos or rename variables that are related to a
bigger change should be squashed into that commit.

This can be done with the following command::

   $ git rebase -i origin/master

This will open up a text editor where you can annotate your commits.

Generally, you'll want to leave the first commit listed as ``pick``, or change
it to ``reword`` (or ``r`` for short) if you want to change the commit message.
And then, if you want to squash every subsequent commit, you could mark them
all as ``fixup`` (or ``f`` for short).

Once you're done, you can check that it worked by running::

   $ git log

If you're happy with the result, do a **force** push (since you're rewriting
history) to your feature branch::

   $ git push -f


See also: http://www.ericbmerritt.com/2011/09/21/commit-hygiene-and-git.html

.. _Homebrew: https://brew.sh
.. _PostgreSQL docs: https://postgresapp.com/
.. _CLA: https://crate.io/community/contribute/agreements/
.. _setuptools-scm: https://pypi.org/project/setuptools-scm/
.. _Semantic Versioning: https://semver.org/
