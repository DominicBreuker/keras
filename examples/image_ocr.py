'''This example uses a convolutional stack followed by a recurrent stack
and a CTC logloss function to perform optical character recognition
of generated text images. I have no evidence of whether it actually
learns general shapes of text, or just is able to recognize all
the different fonts thrown at it...the purpose is more to demonstrate CTC
inside of Keras.  Note that the font list may need to be updated
for the particular OS in use.

This starts off with 4 letter words. After 10 or so epochs, CTC
learns translational invariance, so longer words and groups of words
with spaces are gradually fed in.  This gradual increase in difficulty
is handled using the TextImageGenerator class which is both a generator
class for test/train data and a Keras callback class. Every 10 epochs
the wordlist that the generator draws from increases in difficulty.

The table below shows normalized edit distance values. Theano uses
a slightly different CTC implementation, so some Theano-specific
hyperparameter tuning would be needed to get it to match Tensorflow.

            Norm. ED
Epoch |   TF   |   TH
------------------------
    10   0.072    0.272
    20   0.032    0.115
    30   0.024    0.098
    40   0.023    0.108

This requires cairo and editdistance packages:
pip install cairocffi
pip install editdistance

Due to the use of a dummy loss function, Theano requires the following flags:
on_unused_input='ignore'

Created by Mike Henry
https://github.com/mbhenry/
'''

import os
import itertools
import re
import datetime
import cairocffi as cairo
import editdistance
import numpy as np
from scipy import ndimage
import pylab
from keras import backend as K
from keras.layers.convolutional import Convolution2D, MaxPooling2D
from keras.layers import Input, Layer, Dense, Activation, Flatten
from keras.layers import Reshape, Lambda, merge, Permute, TimeDistributed
from keras.models import Model
from keras.layers.recurrent import GRU
from keras.optimizers import SGD
from keras.utils import np_utils
from keras.utils.data_utils import get_file
from keras.preprocessing import image
import keras.callbacks

OUTPUT_DIR = "image_ocr"

np.random.seed(55)

# this creates larger "blotches" of noise which look
# more realistic than just adding gaussian noise
# assumes greyscale with pixels ranging from 0 to 1

def speckle(img):
    severity = np.random.uniform(0, 0.6)
    blur = ndimage.gaussian_filter(np.random.randn(*img.shape) * severity, 1)
    img_speck = (img + blur)
    img_speck[img_speck > 1] = 1
    img_speck[img_speck <= 0] = 0
    return img_speck

# paints the string in a random location the bounding box
# also uses a random font, a slight random rotation,
# and a random amount of speckle noise

def paint_text(text, w, h):
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    with cairo.Context(surface) as context:
        context.set_source_rgb(1, 1, 1)  # White
        context.paint()
        # this font list works in Centos 7
        fonts = ['Century Schoolbook', 'Courier', 'STIX', 'URW Chancery L', 'FreeMono']
        context.select_font_face(np.random.choice(fonts), cairo.FONT_SLANT_NORMAL,
                                 np.random.choice([cairo.FONT_WEIGHT_BOLD, cairo.FONT_WEIGHT_NORMAL]))
        context.set_font_size(40)
        box = context.text_extents(text)
        if box[2] > w or box[3] > h:
            raise IOError('Could not fit string into image. Max char count is too large for given image width.')

        # teach the RNN translational invariance by
        # fitting text box randomly on canvas, with some room to rotate
        border_w_h = (10, 16)
        max_shift_x = w - box[2] - border_w_h[0]
        max_shift_y = h - box[3] - border_w_h[1]
        top_left_x = np.random.randint(0, int(max_shift_x))
        top_left_y = np.random.randint(0, int(max_shift_y))

        context.move_to(top_left_x - int(box[0]), top_left_y - int(box[1]))
        context.set_source_rgb(0, 0, 0)
        context.show_text(text)

    buf = surface.get_data()
    a = np.frombuffer(buf, np.uint8)
    a.shape = (h, w, 4)
    a = a[:, :, 0]  # grab single channel
    a /= 255
    a = np.expand_dims(a, 0)
    a = speckle(a)
    a = image.random_rotation(a, 3 * (w - top_left_x) / w + 1)

    return a

def shuffle_mats_or_lists(matrix_list, stop_ind=None):
    ret = []
    assert all([len(i) == len(matrix_list[0]) for i in matrix_list])
    len_val = len(matrix_list[0])
    if stop_ind is None:
        stop_ind = len_val
    assert stop_ind <= len_val

    a = range(stop_ind)
    np.random.shuffle(a)
    a += range(stop_ind, len_val)
    for mat in matrix_list:
        if isinstance(mat, np.ndarray):
            ret.append(mat[a])
        elif isinstance(mat, list):
            ret.append([mat[i] for i in a])
        else:
            raise TypeError('shuffle_mats_or_lists only supports numpy.array and list objects')
    return ret

def text_to_labels(text, num_classes):
    ret = []
    for char in text:
        if char >= 'a' and char <= 'z':
            ret.append(ord(char) - ord('a'))
        elif char == ' ':
            ret.append(26)
    return ret

# only a-z and space..probably not to difficult
# to expand to uppercase and symbols

def is_valid_str(in_str):
    search = re.compile(r'[^a-z\ ]').search
    return not bool(search(in_str))

# Uses generator functions to supply train/test with
# data. Image renderings are text are created on the fly
# each time with random perturbations

class TextImageGenerator(keras.callbacks.Callback):

    def __init__(self, monogram_file, bigram_file, minibatch_size, img_w,
                 img_h, downsample_width, val_split,
                 absolute_max_string_len=16):

        self.minibatch_size = minibatch_size
        self.img_w = img_w
        self.img_h = img_h
        self.monogram_file = monogram_file
        self.bigram_file = bigram_file
        self.downsample_width = downsample_width
        self.val_split = val_split
        self.blank_label = self.get_output_size() - 1
        self.absolute_max_string_len = absolute_max_string_len

    def get_output_size(self):
        return 28

    # num_words can be independent of the epoch size due to the use of generators
    # as max_string_len grows, num_words can grow
    def build_word_list(self, num_words, max_string_len=None, mono_fraction=0.5):
        assert max_string_len <= self.absolute_max_string_len
        assert num_words % self.minibatch_size == 0
        assert (self.val_split * num_words) % self.minibatch_size == 0
        self.num_words = num_words
        self.string_list = []
        self.max_string_len = max_string_len
        self.Y_data = np.ones([self.num_words, self.absolute_max_string_len]) * -1
        self.X_text = []
        self.Y_len = [0] * self.num_words

        # monogram file is sorted by frequency in english speech
        with open(self.monogram_file, 'rt') as f:
            for line in f:
                if len(self.string_list) == int(self.num_words * mono_fraction):
                    break
                word = line.rstrip()
                if max_string_len == -1 or max_string_len is None or len(word) <= max_string_len:
                    self.string_list.append(word)

        # bigram file contains common word pairings in english speech
        with open(self.bigram_file, 'rt') as f:
            lines = f.readlines()
            for line in lines:
                if len(self.string_list) == self.num_words:
                    break
                columns = line.lower().split()
                word = columns[0] + ' ' + columns[1]
                if is_valid_str(word) and \
                        (max_string_len == -1 or max_string_len is None or len(word) <= max_string_len):
                    self.string_list.append(word)
        if len(self.string_list) != self.num_words:
            raise IOError('Could not pull enough words from supplied monogram and bigram files. ')

        for i, word in enumerate(self.string_list):
            self.Y_len[i] = len(word)
            self.Y_data[i, 0:len(word)] = text_to_labels(word, self.get_output_size())
            self.X_text.append(word)
        self.Y_len = np.expand_dims(np.array(self.Y_len), 1)

        self.cur_val_index = self.val_split
        self.cur_train_index = 0

    # each time an image is requested from train/val/test, a new random
    # painting of the text is performed
    def get_batch(self, index, size, train):
        X_data = np.ones([size, 1, self.img_h, self.img_w])
        labels = np.ones([size, self.absolute_max_string_len])
        input_length = np.zeros([size, 1])
        label_length = np.zeros([size, 1])
        source_str = []

        for i in range(0, size):
            # Mix in some blank inputs.  This seems to be important for
            # achieving translational invariance
            if train and i > size - 4:
                X_data[i, 0, :, :] = paint_text('', self.img_w, self.img_h)
                labels[i, 0] = self.blank_label
                input_length[i] = self.downsample_width
                label_length[i] = 1
                source_str.append('')
            else:
                X_data[i, 0, :, :] = paint_text(self.X_text[index + i], self.img_w, self.img_h)
                labels[i, :] = self.Y_data[index + i]
                input_length[i] = self.downsample_width
                label_length[i] = self.Y_len[index + i]
                source_str.append(self.X_text[index + i])

        inputs = {'the_input': X_data,
                  'the_labels': labels,
                  'input_length': input_length,
                  'label_length': label_length,
                  'source_str': source_str  # used for visualization only
                  }
        outputs = {'ctc': np.zeros([size])}  # dummy data for dummy loss function
        return (inputs, outputs)

    def next_train(self):
        while 1:
            ret = self.get_batch(self.cur_train_index, self.minibatch_size, train=True)
            self.cur_train_index += self.minibatch_size
            if self.cur_train_index >= self.val_split:
                self.cur_train_index = self.cur_train_index % 32
                (self.X_text, self.Y_data, self.Y_len) = shuffle_mats_or_lists(
                    [self.X_text, self.Y_data, self.Y_len], self.val_split)
            yield ret

    def next_val(self):
        while 1:
            ret = self.get_batch(self.cur_val_index, self.minibatch_size, train=False)
            self.cur_val_index += self.minibatch_size
            if self.cur_val_index >= self.num_words:
                self.cur_val_index = self.val_split + self.cur_val_index % 32
            yield ret

    def on_train_begin(self, logs={}):
        # translational invariance seems to be the hardest thing
        # for the RNN to learn, so start with <= 4 letter words.
        self.build_word_list(16000, 4, 1)

    def on_epoch_begin(self, epoch, logs={}):
        # After 10 epochs, translational invariance should be learned
        # so start feeding longer words and eventually multiple words with spaces
        if epoch == 10:
            self.build_word_list(32000, 8, 1)
        if epoch == 20:
            self.build_word_list(32000, 8, 0.6)
        if epoch == 30:
            self.build_word_list(64000, 12, 0.5)

# the actual loss calc occurs here despite it not being
# an internal Keras loss function

def ctc_lambda_func(args):
    y_pred, labels, input_length, label_length = args
    # the 2 is critical here since the first couple outputs of the RNN
    # tend to be garbage:
    y_pred = y_pred[:, 2:, :]
    return K.ctc_batch_cost(labels, y_pred, input_length, label_length)

# For a real OCR application, this should be beam search with a dictionary
# and language model.  For this example, best path is sufficient.

def decode_batch(test_func, word_batch):
    out = test_func([word_batch])[0]
    ret = []
    for j in range(out.shape[0]):
        out_best = list(np.argmax(out[j, 2:], 1))
        out_best = [k for k, g in itertools.groupby(out_best)]
        # 26 is space, 27 is CTC blank char
        outstr = ''
        for c in out_best:
            if c >= 0 and c < 26:
                outstr += chr(c + ord('a'))
            elif c == 26:
                outstr += ' '
        ret.append(outstr)
    return ret

class VizCallback(keras.callbacks.Callback):

    def __init__(self, test_func, text_img_gen, num_display_words = 6):
        self.test_func = test_func
        self.output_dir = os.path.join(
            OUTPUT_DIR, datetime.datetime.now().strftime('%A, %d. %B %Y %I.%M%p'))
        self.text_img_gen = text_img_gen
        self.num_display_words = num_display_words
        os.makedirs(self.output_dir)

    def show_edit_distance(self, num):
        num_left = num
        mean_norm_ed = 0.0
        mean_ed = 0.0
        while num_left > 0:
            word_batch = next(self.text_img_gen)[0]
            num_proc = min(word_batch['the_input'].shape[0], num_left)
            decoded_res = decode_batch(self.test_func, word_batch['the_input'][0:num_proc])
            for j in range(0, num_proc):
                edit_dist = editdistance.eval(decoded_res[j], word_batch['source_str'][j])
                mean_ed += float(edit_dist)
                mean_norm_ed += float(edit_dist) / len(word_batch['source_str'][j])
            num_left -= num_proc
        mean_norm_ed = mean_norm_ed / num
        mean_ed = mean_ed / num
        print('\nOut of %d samples:  Mean edit distance: %.3f Mean normalized edit distance: %0.3f'
              % (num, mean_ed, mean_norm_ed))

    def on_epoch_end(self, epoch, logs={}):
        self.model.save_weights(os.path.join(self.output_dir, 'weights%02d.h5' % epoch))
        self.show_edit_distance(256)
        word_batch = next(self.text_img_gen)[0]
        res = decode_batch(self.test_func, word_batch['the_input'][0:self.num_display_words])

        for i in range(self.num_display_words):
            pylab.subplot(self.num_display_words, 1, i + 1)
            pylab.imshow(word_batch['the_input'][i, 0, :, :], cmap='Greys_r')
            pylab.xlabel('Truth = \'%s\' Decoded = \'%s\'' % (word_batch['source_str'][i], res[i]))
        fig = pylab.gcf()
        fig.set_size_inches(10, 12)
        pylab.savefig(os.path.join(self.output_dir, 'e%02d.png' % epoch))
        pylab.close()

# Input Parameters
img_h = 64
img_w = 512
nb_epoch = 50
minibatch_size = 32
words_per_epoch = 16000
val_split = 0.2
val_words = int(words_per_epoch * (val_split))

# Network parameters
conv_num_filters = 16
filter_size = 3
pool_size_1 = 4
pool_size_2 = 2
time_dense_size = 32
rnn_size = 512
time_steps = img_w / (pool_size_1 * pool_size_2)

fdir = os.path.dirname(get_file('wordlists.tgz',
                                origin='http://www.isosemi.com/datasets/wordlists.tgz', untar=True))

img_gen = TextImageGenerator(monogram_file=os.path.join(fdir, 'wordlist_mono_clean.txt'),
                             bigram_file=os.path.join(fdir, 'wordlist_bi_clean.txt'),
                             minibatch_size=32,
                             img_w=img_w,
                             img_h=img_h,
                             downsample_width=img_w / (pool_size_1 * pool_size_2) - 2,
                             val_split=words_per_epoch - val_words)

act = 'relu'
input_data = Input(name='the_input', shape=(1, img_h, img_w), dtype='float32')
inner = Convolution2D(conv_num_filters, filter_size, filter_size, border_mode='same',
                      activation=act, input_shape=(1, img_h, img_w), name='conv1')(input_data)
inner = MaxPooling2D(pool_size=(pool_size_1, pool_size_1), name='max1')(inner)
inner = Convolution2D(conv_num_filters, filter_size, filter_size, border_mode='same',
                      activation=act, name='conv2')(inner)
inner = MaxPooling2D(pool_size=(pool_size_2, pool_size_2), name='max2')(inner)

conv_to_rnn_dims = ((img_h / (pool_size_1 * pool_size_2)) * conv_num_filters, img_w / (pool_size_1 * pool_size_2))
inner = Reshape(target_shape=conv_to_rnn_dims, name='reshape')(inner)
inner = Permute(dims=(2, 1), name='permute')(inner)

# cuts down input size going into RNN:
inner = TimeDistributed(Dense(time_dense_size, activation=act, name='dense1'))(inner)

# Two layers of bidirecitonal GRUs
# GRU seems to work as well, if not better than LSTM:
gru_1 = GRU(rnn_size, return_sequences=True, name='gru1')(inner)
gru_1b = GRU(rnn_size, return_sequences=True, go_backwards=True, name='gru1_b')(inner)
gru1_merged = merge([gru_1, gru_1b], mode='sum')
gru_2 = GRU(rnn_size, return_sequences=True, name='gru2')(gru1_merged)
gru_2b = GRU(rnn_size, return_sequences=True, go_backwards=True)(gru1_merged)

# transforms RNN output to character activations:
inner = TimeDistributed(Dense(img_gen.get_output_size(), name='dense2'))(merge([gru_2, gru_2b], mode='concat'))
y_pred = Activation('softmax', name='softmax')(inner)
Model(input=[input_data], output=y_pred).summary()

labels = Input(name='the_labels', shape=[img_gen.absolute_max_string_len], dtype='float32')
input_length = Input(name='input_length', shape=[1], dtype='int64')
label_length = Input(name='label_length', shape=[1], dtype='int64')
# Keras doesn't currently support loss funcs with extra parameters
# so CTC loss is implemented in a lambda layer
loss_out = Lambda(ctc_lambda_func, output_shape=(1,), name="ctc")([y_pred, labels, input_length, label_length])

lr = 0.03
# clipnorm seems to speeds up convergence
clipnorm = 5
sgd = SGD(lr=lr, decay=3e-7, momentum=0.9, nesterov=True, clipnorm=clipnorm)

model = Model(input=[input_data, labels, input_length, label_length], output=[loss_out])

# the loss calc occurs elsewhere, so use a dummy lambda func for the loss
model.compile(loss={'ctc': lambda y_true, y_pred: y_pred}, optimizer=sgd)

# captures output of softmax so we can decode the output during visualization
test_func = K.function([input_data], [y_pred])

viz_cb = VizCallback(test_func, img_gen.next_val())

model.fit_generator(generator=img_gen.next_train(), samples_per_epoch=(words_per_epoch - val_words),
                    nb_epoch=nb_epoch, validation_data=img_gen.next_val(), nb_val_samples=val_words,
                    callbacks=[viz_cb, img_gen])
