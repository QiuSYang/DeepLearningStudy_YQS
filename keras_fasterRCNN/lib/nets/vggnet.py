"""
# 使用Keras搭建faster-rcnn特征提取基础网络Conv_5: vggnet
"""
import os 
import keras 
import keras.models as KM 
import keras.layers as KL 
import keras.backend as KB 


def nnBase(InputShape=(256, 256, 3), Trainable=False):
    image_input = KL.Input(shape=InputShape)

    # block 1 
    x = KL.Conv2D(64, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block1_conv1', trainable=Trainable)(image_input)
    x = KL.Conv2D(64, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block1_conv2', trainable=Trainable)(x)
    x = KL.MaxPool2D((2, 2), strides=(2, 2), name='block1_pool')(x)

    # block 2 
    x = KL.Conv2D(128, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block2_conv1', trainable=Trainable)(x)
    x = KL.Conv2D(128, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block2_conv2', trainable=Trainable)(x)
    x = KL.MaxPool2D((2, 2), strides=(2, 2), name='block2_pool')(x)

    # block 3 
    x = KL.Conv2D(256, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block3_conv1', trainable=Trainable)(x)
    x = KL.Conv2D(256, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block3_conv2', trainable=Trainable)(x)
    x = KL.Conv2D(256, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block3_conv3', trainable=Trainable)(x)
    x = KL.MaxPool2D((2, 2), strides=(2, 2), name='block3_pool')(x)

    # block 4 
    x = KL.Conv2D(512, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block4_conv1', trainable=Trainable)(x)
    x = KL.Conv2D(512, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block4_conv2', trainable=Trainable)(x)
    x = KL.Conv2D(512, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block4_conv3', trainable=Trainable)(x)
    x = KL.MaxPool2D((2, 2), strides=(2, 2), name='block4_pool')(x)

    # block 5
    x = KL.Conv2D(512, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block5_conv1', trainable=Trainable)(x)
    x = KL.Conv2D(512, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block5_conv2', trainable=Trainable)(x)
    x = KL.Conv2D(512, (3, 3), strides=(1, 1), activation='relu', padding='same', 
                name='block5_conv3', trainable=Trainable)(x)
    #x = KL.MaxPool2D((2, 2), strides=(2, 2), name='block5_pool')(x)

    return x, image_input


if __name__ == "__main__":
    feature_output, image_input = nnBase(Trainable=True)

    model = KM.Model(inputs=image_input, outputs=feature_output)

    model.summary() 

    # model graph 
    keras.utils.plot_model(model, to_file='vgg16_model.png')
