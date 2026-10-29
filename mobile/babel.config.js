module.exports = {
  presets: ['module:@react-native/babel-preset'],
  // Obligatoire pour les Frame Processors ('worklet') de VisionCamera v4.
  plugins: [['react-native-worklets-core/plugin']],
};
