# Submit your own plugin

Made a plugin for FTHR Clips? Submit it here for review.

1. Fork this repository and work from the **plugins** branch.
2. Follow the same folder scheme as the existing plugins: create a folder for your plugin and put its installable ZIP inside it.

   ```text
   your-plugin/
     fthr-your-plugin.zip
     README.md
   ```

3. Give your plugin a **name, author, description, unique ID and version** in its `manifest.json`. Include the appropriate `api_version`, `type` and `contributes` fields. The manifest belongs at the root of the ZIP. See the [plugin guide](guide/plugins.md#package-format) for the format.
4. In your plugin's README, briefly explain what it does, how to install and use it, which FTHR Clips version you tested, and any requirements or known limitations.
5. Test the ZIP in FTHR Clips, then open a **pull request targeting this repository's `plugins` branch**. Describe your plugin and what you tested.

We will review your submission and either approve or decline it. We may ask for changes before approving. Opening a pull request does not guarantee that your plugin will be included.

For updates, use the same folder and plugin ID, increase the version, and submit another pull request.
