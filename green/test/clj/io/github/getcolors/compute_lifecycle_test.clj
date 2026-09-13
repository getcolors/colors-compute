(ns io.github.getcolors.compute-lifecycle-test
  (:require [clojure.test :refer [deftest is testing]]
            [cheshire.core :as json]
            [clojure.walk :as walk]
            [io.github.getcolors.compute-lifecycle :as l]))
(defn normalize [value]
  (walk/postwalk #(if (and (number? %) (== % (Math/floor (double %)))) (long %) %)
                 (json/parse-string-strict (json/generate-string value) true)))
(deftest schema-two-shared-fixtures
  (doseq [{:keys [name op args expected]} (json/parse-string-strict (slurp "../test/fixtures/lifecycle.json") true)]
    (testing name
      (let [before (pr-str args) f (if (= op "lifecycle_repair") l/repair l/lifecycle)
            result (try (apply f args) (catch Exception e {:error (.getMessage e)}))]
        (is (= (normalize expected) (normalize result)))
        (is (= before (pr-str args)))
        (when (:document result) (is (l/valid-document? (:document result))))))))
