# 公開版の来歴

`wickward`に収録されている計画エンジンから、duration input、availability v2、preplan handoff、unified navigation runtimeの4系統を抽出しました。
実行に使うPythonソースは変更していません。
抽出時のGit treeとファイル単位のハッシュは[source-manifest.json](source-manifest.json)にあります。
エンジン内部のソース照合用マニフェストも維持しています。

公開用に追加したものは、7日分の合成CSV、中央値による予測、入力変換、HTML出力とそのテストです。
これらは`demo.py`、`data/`、`tests/`に分けています。
元の作業用READMEや、観測記録、保存済みの運用結果、地図画像は含めていません。
エンジンが参照する施設識別用の静的な対応表は残していますが、デモでは架空の施設IDを使用します。

施設の体験時間は公開デモ用の仮定です。
入力の`source_url`は`example.invalid`、`source_sha256`はゼロ埋めで、実際の出典を示すものではありません。
この区別は画面とデータ内にも記載しています。

履歴を引き継がず、新規リポジトリに必要なファイルだけを追加しています。
