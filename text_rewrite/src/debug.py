"""
# debug模块
"""
import os
import tensorflow as tf


def load_checkpoints(checkpoint_path):
    tf_path = os.path.abspath(checkpoint_path)
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        print("Loading TF weight {} with shape {}".format(name, shape))
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array)


def freeze_graph(input_checkpoint, output_graph):
    '''
    :param input_checkpoint:
    :param output_graph: PB模型保存路径
    :return:
    '''
    # 指定输出的节点名称,该节点名称必须是原模型中存在的节点
    # 直接用最后输出的节点，可以在tensorboard中查找到，tensorboard只能在linux中使用
    output_node_names = "decode/distribute_layer"
    graph = tf.compat.v1.get_default_graph()  # 获得默认的图
    input_graph_def = graph.as_graph_def()  # 返回一个序列化的图代表当前的图
    saver = tf.compat.v1.train.import_meta_graph(input_checkpoint + '.meta', clear_devices=True)

    with tf.Session() as sess:
        saver.restore(sess, input_checkpoint)  # 恢复图并得到数据
        output_graph_def = tf.graph_util.convert_variables_to_constants(  # 模型持久化，将变量值固定
            sess=sess,
            input_graph_def=input_graph_def,  # 等于:sess.graph_def
            output_node_names=output_node_names.split(","))  # 如果有多个输出节点，以逗号隔开

        with tf.gfile.GFile(output_graph, "wb") as f:  # 保存模型
            f.write(output_graph_def.SerializeToString())  # 序列化输出
        print("%d ops in the final graph." % len(output_graph_def.node))  # 得到当前图有几个操作节点


def load_model(input_checkpoint):
    tf.compat.v1.disable_eager_execution()
    with tf.compat.v1.Session() as sess:
        # saver = tf.compat.v1.train.Saver()
        saver = tf.compat.v1.train.import_meta_graph(input_checkpoint + '.meta', clear_devices=True)
        saver.restore(sess, input_checkpoint)
        print(sess.run('model/transformer/embedding_and_softmax/weights:0'))
        pass


def requests_server():
    import requests
    import json
    data = {"id": 1, "contexts": ["你知道板泉井水吗", "知道", "她是歌手"]}
    data = json.dumps(data)  # encoder
    r = requests.post("http://10.128.61.27:8280/rewrite", data=data)
    print(json.loads(r.text))


if __name__ == '__main__':
    # checkpoint_path = tf.train.latest_checkpoint("../models/tiny_custom")  # "transformer/model_tiny/model.ckpt-282"
    # print("last model path: {}".format(checkpoint_path))
    # # load_model(checkpoint_path)
    # # checkpoint_path = "/home/yckj2453/nlp_space/tf-1-codes/dialogue-utterance-rewriter/transformer/model_tiny/model.ckpt-282"
    # # checkpoint_path = "/home/yckj2609/project/KBQA/multi-dialoque-ir/dialogue-rewriter-master/transformer/generated_model/model.ckpt-5630"
    # # freeze_graph(checkpoint_path, "model_tiny_x")
    # load_checkpoints(checkpoint_path)

    requests_server()
