(ns io.github.getcolors.compute-ssh-cleanup-test
  (:require [clojure.test :refer [deftest is]]
            [io.github.getcolors.compute-ssh]))

(deftest repeated-interruption-drains-one-cleanup
  (let [process (.start (ProcessBuilder. ["sh" "-c" "cat >/dev/null; sleep 0.1"]))
        cleaned (atom 0)
        output (future (slurp (.getInputStream process)))
        errors (future (slurp (.getErrorStream process)))
        close! (#'io.github.getcolors.compute-ssh/process-closer
                 process nil output errors #(swap! cleaned inc))
        entered (promise)
        runner (Thread. (fn [] (deliver entered true) (close!)))]
    (.start runner)
    @entered
    (.interrupt runner)
    (Thread/sleep 20)
    (.interrupt runner)
    (.join runner 2000)
    (is (not (.isAlive runner)))
    (is (not (.isAlive process)))
    (is (= 1 @cleaned))
    (close!)
    (is (= 1 @cleaned))))
